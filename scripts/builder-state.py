#!/usr/bin/env python3
"""Export/restore encrypted macOS builder credentials and Buildkite configuration."""
import argparse
import base64
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

REPO = Path(__file__).resolve().parent.parent
DEFAULT_IDENTITY = Path.home() / 'Library/Application Support/Divvun Builder/backup.agekey'


def private_file(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError('Secret file must be owned by you and mode 0600: ' + str(path))


def outside_repo(path):
    path = path.expanduser().resolve()
    if path == REPO or REPO in path.parents:
        raise ValueError('Keep credential backups and decryption keys outside the repository')
    return path


def checked(args, input_data=None):
    p = subprocess.run(args, input=input_data, capture_output=True)
    if p.returncode:
        raise RuntimeError('Command failed: ' + Path(args[0]).name)
    return p.stdout


def identity_file(path, create=False):
    path = outside_repo(path)
    if not path.exists():
        if not create:
            raise ValueError('Decryption identity does not exist: ' + str(path))
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # age-keygen refuses to overwrite an existing output file.
        checked(['age-keygen', '-o', str(path)])
        path.chmod(0o600)
        print('Created backup identity: ' + str(path), file=sys.stderr)
        print('Store a secure copy of this file separately from the encrypted backup.', file=sys.stderr)
    private_file(path)
    return path


def remote(target, jump, request, host_alias=None):
    if target.startswith('-') or any(c.isspace() for c in target):
        raise ValueError('Use an SSH host or user@host target')
    helper = (REPO / 'scripts/builder-state-guest.py').read_bytes()
    encoded = base64.b64encode(helper).decode()
    # Only program code appears in SSH argv. Credentials and configuration travel
    # over stdin; plaintext bundles are never written to the controller's disk.
    bootstrap = (
        'import base64,pathlib,subprocess,sys,tempfile\n'
        'with tempfile.TemporaryDirectory(prefix="divvun-builder-state-") as directory:\n'
        ' path=pathlib.Path(directory)/"guest.py"\n'
        ' path.write_bytes(base64.b64decode(' + repr(encoded) + '))\n'
        ' result=subprocess.run(["/usr/bin/python3",str(path)],input=sys.stdin.buffer.read())\n'
        ' sys.exit(result.returncode)\n'
    )
    args = ['ssh', '-o', 'ConnectTimeout=15']
    if jump:
        args += ['-J', jump]
    if host_alias:
        args += ['-o', 'HostKeyAlias=' + host_alias, '-o', 'StrictHostKeyChecking=accept-new']
    args += [target, '/usr/bin/python3 -c ' + shlex.quote(bootstrap)]
    p = subprocess.run(args, input=json.dumps(request).encode(), stdout=subprocess.PIPE)
    if p.returncode:
        raise RuntimeError('Guest operation failed; no successful migration was reported')
    return json.loads(p.stdout)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    for name in ['export', 'restore', 'verify']:
        cmd = sub.add_parser(name)
        target = cmd.add_mutually_exclusive_group(required=True)
        target.add_argument('--host', help='admin@VM_IP or an SSH config alias')
        target.add_argument('--vm', help='local Tart VM name; resolves its IP and uses a stable SSH host-key alias')
        cmd.add_argument('--jump', help='optional SSH jump host, e.g. brendan@mac-host')
        if name != 'verify':
            cmd.add_argument('--identity', type=Path, default=DEFAULT_IDENTITY, help='age decryption key; created on first export')
        if name == 'export':
            cmd.add_argument('--output', required=True, type=Path, help='new .age backup outside the repository')
            cmd.add_argument('--keychain', action='append', default=[], help='limit export to these guest keychain paths')
            cmd.add_argument('--unlock-file', type=Path, help='mode-0600 JSON map of guest keychain paths to passwords, if locked')
        if name == 'restore':
            cmd.add_argument('--input', required=True, type=Path)
            cmd.add_argument('--start-agent', action='store_true', help='start Buildkite after signing verification succeeds')
    args = parser.parse_args()
    alias = None
    if args.vm:
        if args.jump:
            raise ValueError('Run --vm on the Tart host; use --host with --jump from another machine')
        if not re.fullmatch('[A-Za-z0-9_.:-]+', args.vm) or args.vm.startswith('-'):
            raise ValueError('Invalid Tart VM name')
        address = checked(['tart', 'ip', args.vm]).decode().strip()
        if not address:
            raise RuntimeError('The VM must be running before migration')
        args.host = 'admin@' + address
        alias = args.vm + '.tart'
    if args.action == 'verify':
        print(json.dumps(remote(args.host, args.jump, {'action': 'verify'}, alias), indent=2))
        return
    for tool in ['age', 'age-keygen']:
        if not shutil.which(tool):
            raise RuntimeError('Install the encryption tools on the controller: brew install age')
    if args.action == 'export':
        output = outside_repo(args.output)
        if output.exists():
            raise ValueError('Backup already exists; choose a new output filename')
        if not output.parent.is_dir():
            raise ValueError('Create the backup directory before exporting: ' + str(output.parent))
        key = identity_file(args.identity, create=True)
        recipient = checked(['age-keygen', '-y', str(key)]).decode().strip()
        request = {'action': 'export', 'keychains': args.keychain}
        if args.unlock_file:
            private_file(args.unlock_file)
            request['unlock'] = json.loads(args.unlock_file.read_text())
        bundle = remote(args.host, args.jump, request, alias)
        encrypted = checked(['age', '-r', recipient], json.dumps(bundle).encode())
        # Exclusive creation prevents a concurrent export from being overwritten.
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(encrypted)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            output.unlink(missing_ok=True)
            raise
        print('Encrypted backup saved: ' + str(output))
        print('Signing identities: ' + str(len({f for entry in bundle['identities'] for f in entry['fingerprints']})))
    else:
        key = identity_file(args.identity)
        plaintext = checked(['age', '-d', '-i', str(key), str(args.input.expanduser())])
        bundle = json.loads(plaintext)
        result = remote(args.host, args.jump, {'action': 'restore', 'bundle': bundle, 'start_agent': args.start_agent}, alias)
        print(json.dumps(result, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print('Builder state: ' + (str(error) or 'interrupted'), file=sys.stderr)
        sys.exit(1)
