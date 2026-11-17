#!/usr/bin/env python3
"""Guest-side implementation for builder-state.py; secrets travel on stdin only."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import uuid

HOME_DIR = Path.home()
STATE_DIR = HOME_DIR / '.config/divvun-builder'
BREW = Path('/opt/homebrew')
AGENT_DIR = BREW / 'etc/buildkite-agent'
SECURITY = '/usr/bin/security'


def run(args, input_data=None):
    p = subprocess.run(args, input=input_data, capture_output=True, timeout=120)
    if p.returncode:
        # Do not echo command arguments or output: they may contain credentials.
        error = RuntimeError('Command failed: ' + Path(args[0]).name + ' ' + str(args[1] if len(args) > 1 else ''))
        error.stderr = p.stderr
        raise error
    return p.stdout


def security_private(args):
    # security's -i parser is not a shell. Quote every argument using its own
    # double-quote/backslash rules, and execute one command per process so an
    # earlier failure cannot be hidden by a later success. No passwords in argv.
    values = [str(v) for v in args]
    if any(any(ord(c) < 32 for c in v) for v in values):
        raise ValueError('Control characters are not supported in keychain inputs')
    line = ' '.join('"' + v.replace('\\', '\\\\').replace('"', '\\"') + '"' for v in values) + '\n'
    if len(line.encode()) >= 4096 or len(values) > 32:
        raise ValueError('Keychain command exceeds security tool limits')
    return run([SECURITY, '-i'], line.encode())


def identities(keychain=None, valid=True):
    args = [SECURITY, 'find-identity'] + (['-v'] if valid else []) + ['-p', 'codesigning']
    if keychain:
        args.append(str(keychain))
    text = run(args).decode()
    return sorted(set(re.findall(r'^\s*\d+\) ([A-Fa-f0-9]{40}) ', text, re.M)))


def search_list():
    text = run([SECURITY, 'list-keychains', '-d', 'user']).decode()
    return [line.strip().strip('"') for line in text.splitlines() if line.strip()]


def secure_dir(path):
    if path.is_symlink():
        raise ValueError('Refusing symlink directory: ' + str(path))
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_uid != os.getuid():
        raise ValueError('Directory is owned by another user: ' + str(path))
    path.chmod(0o700)


def write_private(path, data, mode=0o600):
    if path.is_symlink():
        raise ValueError('Refusing symlink destination: ' + str(path))
    fd, name = tempfile.mkstemp(prefix='.builder-state-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def encode_file(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('Expected a regular file: ' + str(path))
    return base64.b64encode(path.read_bytes()).decode()


def export_state(options):
    keychains = options.get('keychains') or search_list()
    for path, password in options.get('unlock', {}).items():
        security_private(['unlock-keychain', '-p', password, path])
    wrapping_password = secrets.token_hex(32)
    bundle = {'schema': 1, 'id': uuid.uuid4().hex, 'source_home': str(HOME_DIR),
              'source_brew': str(BREW), 'p12_password': wrapping_password,
              'identities': [], 'ssh': {}, 'hooks': {}, 'environment': {}}
    with tempfile.TemporaryDirectory(prefix='divvun-key-export-') as temp:
        for index, keychain in enumerate(keychains):
            fingerprints = identities(keychain)
            if not fingerprints:
                continue
            output = Path(temp) / (str(index) + '.p12')
            try:
                security_private(['export', '-k', keychain, '-t', 'identities', '-f', 'pkcs12',
                                  '-P', wrapping_password, '-o', str(output)])
            except RuntimeError:
                raise RuntimeError('Cannot export signing identities from ' + str(keychain) + '. Unlock that keychain and permit key export in the guest, then retry.') from None
            if not output.is_file() or not output.stat().st_size:
                raise RuntimeError('Keychain export produced no identities')
            bundle['identities'].append({'fingerprints': fingerprints, 'p12': encode_file(output)})
    if not bundle['identities']:
        raise RuntimeError('No valid signing identities found. Unlock the source signing keychain and retry; no backup was created.')
    config = AGENT_DIR / 'buildkite-agent.cfg'
    if not config.is_file():
        raise RuntimeError('Homebrew Buildkite configuration is missing')
    bundle['buildkite_config'] = encode_file(config)
    for p in sorted((AGENT_DIR / 'hooks').glob('*')):
        if p.is_file() and '.sample' not in p.name:
            if p.name == 'environment' and '# Managed by divvun-builder.' in p.read_text():
                original = STATE_DIR / 'source-environment'
                if original.is_file():
                    if '# Managed by divvun-builder.' in original.read_text():
                        raise RuntimeError('Managed environment hook cannot source itself')
                    bundle['hooks'][p.name] = encode_file(original)
            else:
                bundle['hooks'][p.name] = encode_file(p)
    for p in sorted((HOME_DIR / '.ssh').glob('id_*')):
        if p.is_file():
            bundle['ssh'][p.name] = encode_file(p)
    for relative in ['.local/bin/env', '.config/divvun-builder/environment']:
        path = HOME_DIR / relative
        if path.is_file():
            bundle['environment'][relative] = encode_file(path)
    return bundle


def decode(value):
    return base64.b64decode(value, validate=True)


def validate_bundle(bundle):
    if bundle.get('schema') != 1 or not re.fullmatch('[0-9a-f]{32}', bundle.get('id', '')):
        raise ValueError('Unsupported or malformed backup')
    if not bundle.get('identities'):
        raise ValueError('Backup contains no signing identities')
    expected = set()
    for identity in bundle['identities']:
        decode(identity['p12'])
        for fingerprint in identity['fingerprints']:
            if not re.fullmatch('[A-Fa-f0-9]{40}', fingerprint):
                raise ValueError('Invalid signing identity fingerprint')
            expected.add(fingerprint.upper())
    if not expected:
        raise ValueError('Backup contains no signing identity fingerprints')
    for key in ['source_home', 'source_brew']:
        if not isinstance(bundle.get(key), str) or not bundle[key].startswith('/') or bundle[key] == '/':
            raise ValueError('Invalid source path in backup')
    if not isinstance(bundle.get('p12_password'), str) or not bundle['p12_password']:
        raise ValueError('Missing PKCS#12 wrapping password')
    decode(bundle['buildkite_config'])
    for group in ['ssh', 'hooks']:
        for name, content in bundle[group].items():
            if not re.fullmatch('[A-Za-z0-9_.-]+', name) or name in ('.', '..'):
                raise ValueError('Invalid backup filename')
            if group == 'ssh' and not name.startswith('id_'):
                raise ValueError('Only SSH identity files may be restored')
            decode(content)
    for relative, content in bundle['environment'].items():
        if relative not in ['.local/bin/env', '.config/divvun-builder/environment']:
            raise ValueError('Unexpected environment file')
        decode(content)
    return expected


def unlock_state():
    path = STATE_DIR / 'signing.json'
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError('Signing state must be owned by the current user with mode 0600')
    state = json.loads(path.read_text())
    security_private(['unlock-keychain', '-p', state['password'], state['keychain']])
    return state


def verify_signing(state, expected):
    security_private(['unlock-keychain', '-p', state['password'], state['keychain']])
    present = {v.upper() for v in identities(state['keychain'])}
    if not expected <= present:
        raise RuntimeError('Restored keychain is missing valid signing identities')
    with tempfile.TemporaryDirectory(prefix='divvun-sign-test-') as temp:
        for fingerprint in sorted(expected):
            target = Path(temp) / 'sign-test'
            shutil.copyfile('/usr/bin/true', target)
            run(['/usr/bin/codesign', '--force', '--sign', fingerprint, '--keychain', state['keychain'], '--timestamp=none', str(target)])
            run(['/usr/bin/codesign', '--verify', '--strict', str(target)])


def restore_state(bundle, start_agent=False):
    expected = validate_bundle(bundle)
    if run(['/usr/bin/id', '-u']).strip() == b'0':
        raise ValueError('Run restore as the builder user, not root')
    if subprocess.run(['/usr/bin/pgrep', '-x', 'buildkite-agent'], capture_output=True).returncode == 0:
        raise RuntimeError('Stop the destination Buildkite agents before restoring configuration')
    if not AGENT_DIR.is_dir() or not os.access(AGENT_DIR, os.W_OK):
        raise RuntimeError('Install Buildkite for this user before restoring')
    if not (BREW / 'bin/buildkite-agent').exists():
        raise RuntimeError('Buildkite agent executable is missing')
    secure_dir(STATE_DIR)
    secure_dir(HOME_DIR / '.ssh')
    keychain = HOME_DIR / 'Library/Keychains' / ('divvun-builder-' + bundle['id'] + '.keychain-db')
    recovery_path = STATE_DIR / ('restore-' + bundle['id'] + '.json')
    if recovery_path.exists():
        if recovery_path.is_symlink() or recovery_path.stat().st_mode & 0o077:
            raise ValueError('Restore state has unsafe permissions')
        state = json.loads(recovery_path.read_text())
        if state['keychain'] != str(keychain):
            raise ValueError('Restore state does not match the backup')
    else:
        if keychain.exists():
            raise RuntimeError('Keychain already exists without matching restore state')
        state = {'keychain': str(keychain), 'password': secrets.token_hex(32), 'bundle_id': bundle['id'], 'fingerprints': sorted(expected)}
        write_private(recovery_path, json.dumps(state).encode())
    if not keychain.exists():
        security_private(['create-keychain', '-p', state['password'], str(keychain)])
    security_private(['unlock-keychain', '-p', state['password'], str(keychain)])
    security_private(['set-keychain-settings', '-t', '21600', str(keychain)])
    with tempfile.TemporaryDirectory(prefix='divvun-key-import-') as temp:
        for index, identity in enumerate(bundle['identities']):
            present = {v.upper() for v in identities(keychain, valid=False)}
            if {v.upper() for v in identity['fingerprints']} <= present:
                continue
            p12 = Path(temp) / (str(index) + '.p12')
            p12.write_bytes(decode(identity['p12']))
            security_private(['import', str(p12), '-k', str(keychain), '-f', 'pkcs12',
                              '-P', bundle['p12_password'], '-T', '/usr/bin/codesign', '-T', '/usr/bin/security',
                              '-T', '/usr/bin/productsign', '-T', '/usr/bin/productbuild'])
    security_private(['set-key-partition-list', '-S', 'apple-tool:,apple:,codesign:', '-s', '-k', state['password'], str(keychain)])
    current = search_list()
    if str(keychain) not in current:
        run([SECURITY, 'list-keychains', '-d', 'user', '-s'] + current + [str(keychain)])
    verify_signing(state, expected)

    backups = STATE_DIR / 'backups' / bundle['id']
    secure_dir(backups)

    def install(path, content, mode=0o600):
        if path.is_symlink():
            raise ValueError('Refusing symlink destination: ' + str(path))
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_bytes() != content:
            backup = backups / hashlib.sha256(str(path).encode()).hexdigest()
            if not backup.exists():
                write_private(backup, path.read_bytes())
                write_private(backup.with_suffix('.path'), str(path).encode())
        write_private(path, content, mode)

    def relocate(content):
        return content.replace(bundle['source_home'].encode(), str(HOME_DIR).encode()).replace(bundle['source_brew'].encode(), str(BREW).encode())

    install(AGENT_DIR / 'buildkite-agent.cfg', relocate(decode(bundle['buildkite_config'])))
    for name, content in bundle['ssh'].items():
        install(HOME_DIR / '.ssh' / name, decode(content))
    for relative, content in bundle['environment'].items():
        install(HOME_DIR / relative, relocate(decode(content)))
    for name, content in bundle['hooks'].items():
        # A custom environment hook runs before the managed signing-unlock hook.
        target = STATE_DIR / 'source-environment' if name == 'environment' else AGENT_DIR / 'hooks' / name
        install(target, relocate(decode(content)), 0o700)
    if 'environment' not in bundle['hooks']:
        install(STATE_DIR / 'source-environment', b'#!/bin/bash\n', 0o700)
    install(STATE_DIR / 'keychain-helper.py', Path(__file__).read_bytes(), 0o700)
    install(STATE_DIR / 'signing.json', json.dumps(state).encode())
    install(AGENT_DIR / 'hooks/environment', MANAGED_ENVIRONMENT.encode(), 0o755)
    if start_agent:
        run([str(BREW / 'bin/brew'), 'services', 'start', 'buildkite/buildkite/buildkite-agent@3'])
    return {'restored_signing_identities': len(expected), 'ssh_identity_files': len(bundle['ssh']), 'agent_started': start_agent}


MANAGED_ENVIRONMENT = '''#!/bin/bash
# Managed by divvun-builder.
# Buildkite sources this hook for every job. Keep sourced secrets out of xtrace.
_divvun_restore_xtrace=false
case "$-" in *x*) _divvun_restore_xtrace=true; set +x ;; esac
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$PATH"
if [[ -r "$HOME/.local/bin/env" ]]; then
  source "$HOME/.local/bin/env" || return 1
fi
if [[ -r "$HOME/.config/divvun-builder/source-environment" ]]; then
  source "$HOME/.config/divvun-builder/source-environment" || return 1
fi
if [[ -r "$HOME/.config/divvun-builder/environment" ]]; then
  source "$HOME/.config/divvun-builder/environment" || return 1
fi
if [[ -f "$HOME/.config/divvun-builder/signing.json" ]]; then
  /usr/bin/python3 "$HOME/.config/divvun-builder/keychain-helper.py" unlock || return 1
fi
if [[ "$_divvun_restore_xtrace" == true ]]; then
  unset _divvun_restore_xtrace
  set -x
else
  unset _divvun_restore_xtrace
fi
'''


def main():
    os.umask(0o077)
    if len(sys.argv) == 2 and sys.argv[1] == 'unlock':
        unlock_state()
        return
    request = json.load(sys.stdin)
    if request['action'] == 'export':
        result = export_state(request)
    elif request['action'] == 'restore':
        result = restore_state(request['bundle'], request.get('start_agent', False))
    elif request['action'] == 'verify':
        state = unlock_state()
        verify_signing(state, set(state['fingerprints']))
        result = {'verified_signing_identities': len(state['fingerprints'])}
    else:
        raise ValueError('Unknown action')
    print(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Builder state: ' + str(error), file=sys.stderr)
        sys.exit(1)
