import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guest = load('guest', 'builder-state-guest.py')
controller = load('controller', 'builder-state.py')


def encoded(value):
    return base64.b64encode(value).decode()


def example_bundle():
    return {'schema': 1, 'id': 'a' * 32, 'source_home': '/Users/admin',
            'source_brew': '/opt/homebrew', 'p12_password': 'fixture-only',
            'identities': [{'fingerprints': ['A' * 40], 'p12': encoded(b'p12-fixture')}],
            'buildkite_config': encoded(b'token="fixture-agent-token"\nspawn=2\n'),
            'ssh': {'id_ed25519': encoded(b'fixture-private-ssh-key')},
            'hooks': {'environment': encoded(b'export FIXTURE_SETTING=ready\n')}, 'environment': {}}


class ValidationTests(unittest.TestCase):
    def test_path_traversal_rejected_before_restore(self):
        for group, path in [('ssh', '../authorized_keys'), ('hooks', '../buildkite-agent.cfg'), ('environment', '.ssh/authorized_keys')]:
            bundle = example_bundle()
            bundle[group][path] = encoded(b'bad')
            with self.subTest(group=group), self.assertRaises(ValueError):
                guest.validate_bundle(bundle)

    def test_bad_identity_rejected(self):
        bundle = example_bundle()
        bundle['identities'][0]['fingerprints'] = ['bad']
        with self.assertRaises(ValueError):
            guest.validate_bundle(bundle)

    def test_secret_is_stdin_only(self):
        secret = 'fixture "quotes" and \\ backslash'
        with mock.patch.object(guest, 'run', return_value=b'') as run:
            guest.security_private(['unlock-keychain', '-p', secret, '/tmp/a keychain'])
        args, data = run.call_args.args
        self.assertEqual(args, ['/usr/bin/security', '-i'])
        self.assertNotIn(secret, ' '.join(args))
        self.assertIn(b'\\"quotes\\"', data)
        with self.assertRaises(ValueError):
            guest.security_private(['unlock-keychain', '-p', 'bad\ncommand'])

    def test_hook_matches_image(self):
        self.assertEqual(guest.MANAGED_ENVIRONMENT, (ROOT / 'data/buildkite-environment.sh').read_text())
        result = subprocess.run(['/bin/bash', '-n', str(ROOT / 'data/buildkite-environment.sh')], capture_output=True)
        self.assertEqual(result.returncode, 0)

    def test_environment_secrets_are_not_traced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / '.config/divvun-builder'
            state.mkdir(parents=True)
            (state / 'environment').write_text('export FIXTURE_SECRET="sensitive-fixture-value"\n')
            hook = root / 'hook.sh'
            # Substitute only the filesystem root for the isolated fixture;
            # leave the real HOME variable and environment untouched.
            hook.write_text(guest.MANAGED_ENVIRONMENT.replace('$HOME', directory))
            command = 'source "$1"; case "$-" in *x*) echo trace-restored;; *) exit 1;; esac; test "${FIXTURE_SECRET:+present}" = present'
            result = subprocess.run(['/bin/bash', '-x', '-c', command, 'test', str(hook)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertIn(b'trace-restored', result.stdout)
            self.assertNotIn(b'sensitive-fixture-value', result.stderr)

    def test_reexport_preserves_original_hook_without_recursion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / 'agent'; (agent / 'hooks').mkdir(parents=True)
            state = root / 'state'; state.mkdir()
            (agent / 'buildkite-agent.cfg').write_text('token="fixture"\n')
            (agent / 'hooks/environment').write_text(guest.MANAGED_ENVIRONMENT)
            original = b'export MY_BUILDER_SETTING=1\n'
            (state / 'source-environment').write_bytes(original)
            def export(args):
                Path(args[args.index('-o') + 1]).write_bytes(b'fixture-p12')
            with mock.patch.multiple(guest, HOME_DIR=root, STATE_DIR=state, AGENT_DIR=agent), mock.patch.object(guest, 'identities', return_value=['A' * 40]), mock.patch.object(guest, 'security_private', side_effect=export):
                bundle = guest.export_state({'keychains': ['fixture-keychain']})
            self.assertEqual(guest.decode(bundle['hooks']['environment']), original)


@unittest.skipUnless(shutil.which('age') and shutil.which('age-keygen'), 'age is required')
class EncryptionTests(unittest.TestCase):
    def test_export_restore_roundtrip_and_wrong_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = root / 'identity.agekey'
            archive = root / 'backup.age'
            bundle = example_bundle()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with mock.patch.object(sys, 'argv', ['builder-state.py', 'export', '--host', 'fixture', '--identity', str(key), '--output', str(archive)]), mock.patch.object(controller, 'remote', return_value=bundle):
                    controller.main()
                ciphertext = archive.read_bytes()
                self.assertNotIn(b'fixture-agent-token', ciphertext)
                self.assertNotIn(b'fixture-private-ssh-key', ciphertext)
                self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
                self.assertEqual(key.stat().st_mode & 0o777, 0o600)
                with mock.patch.object(sys, 'argv', ['builder-state.py', 'restore', '--host', 'fixture', '--identity', str(key), '--input', str(archive)]), mock.patch.object(controller, 'remote', return_value={'ok': True}) as remote:
                    controller.main()
                self.assertEqual(remote.call_args.args[2]['bundle'], bundle)
                other = controller.identity_file(root / 'wrong.agekey', create=True)
                with self.assertRaises(RuntimeError):
                    controller.checked(['age', '-d', '-i', str(other), str(archive)])
                archive.write_bytes(ciphertext[:-4] + b'bad!')
                with self.assertRaises(RuntimeError):
                    controller.checked(['age', '-d', '-i', str(key), str(archive)])


@unittest.skipUnless(sys.platform == 'darwin' and os.environ.get('RUN_KEYCHAIN_TESTS') == '1', 'opt-in macOS keychain integration test')
class KeychainIntegrationTests(unittest.TestCase):
    def test_private_key_export_import_sign_relock_and_repeat(self):
        os.umask(0o077)
        with tempfile.TemporaryDirectory(prefix='builder-key-test-') as directory:
            root = Path(directory)
            source = root / 'source.keychain-db'
            home = root / 'home'; (home / 'Library/Keychains').mkdir(parents=True)
            state_dir = home / '.config/divvun-builder'
            brew = root / 'brew'; (brew / 'bin').mkdir(parents=True)
            (brew / 'bin/buildkite-agent').write_text('fixture')
            agent = brew / 'etc/buildkite-agent'; (agent / 'hooks').mkdir(parents=True)
            (agent / 'buildkite-agent.cfg').write_text('token="old-fixture"\n')
            (home / '.ssh').mkdir(); (home / '.ssh/id_ed25519').write_text('old-fixture-key')
            source_password = 'fixture "quote" \\ password'
            openssl = '/opt/homebrew/opt/openssl@3/bin/openssl'
            cert = root / 'cert.pem'; private_key = root / 'key.pem'; p12 = root / 'fixture.p12'
            wrapping = 'fixture-wrapper'
            created = []
            original_identities = guest.identities
            try:
                guest.security_private(['create-keychain', '-p', source_password, str(source)])
                created.append(source)
                guest.run([openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(private_key), '-out', str(cert), '-subj', '/CN=Divvun migration disposable test', '-days', '1', '-addext', 'extendedKeyUsage=critical,codeSigning', '-addext', 'basicConstraints=critical,CA:FALSE', '-addext', 'keyUsage=critical,digitalSignature'])
                guest.run([openssl, 'pkcs12', '-export', '-legacy', '-inkey', str(private_key), '-in', str(cert), '-out', str(p12), '-passout', 'stdin'], (wrapping + '\n').encode())
                guest.security_private(['import', str(p12), '-k', str(source), '-f', 'pkcs12', '-P', wrapping, '-T', '/usr/bin/security', '-T', '/usr/bin/codesign'])
                guest.security_private(['set-key-partition-list', '-S', 'apple-tool:,apple:,codesign:', '-s', '-k', source_password, str(source)])
                fingerprints = original_identities(source, valid=False)
                self.assertTrue(fingerprints, 'Fixture private key was not imported')
                # The fixture is self-signed. Allow it in identity enumeration
                # without modifying this machine's certificate trust settings.
                with mock.patch.object(guest, 'identities', side_effect=lambda keychain=None, valid=True: original_identities(keychain, valid=False)), mock.patch.multiple(guest, HOME_DIR=home, STATE_DIR=state_dir, BREW=brew, AGENT_DIR=agent):
                    (agent / 'hooks/environment').write_text('export FIXTURE_SETTING=ready\n')
                    bundle = guest.export_state({'keychains': [str(source)]})
                    self.assertTrue(bundle['identities'])
                    bundle['ssh'] = {'id_ed25519': encoded(b'restored-fixture-key')}
                    expected_keychain = home / 'Library/Keychains' / ('divvun-builder-' + bundle['id'] + '.keychain-db')
                    created.append(expected_keychain)
                    try:
                        result = guest.restore_state(bundle)
                    except RuntimeError as error:
                        if str(error).startswith('Command failed: codesign'):
                            print(error.stderr.decode(), file=sys.stderr)
                            print('Fixture trusted identities:', original_identities(expected_keychain, valid=True), file=sys.stderr)
                            print('Fixture matching identities:', original_identities(expected_keychain, valid=False), file=sys.stderr)
                        raise
                    self.assertEqual(result['restored_signing_identities'], len(fingerprints))
                    self.assertEqual((home / '.ssh/id_ed25519').read_bytes(), b'restored-fixture-key')
                    self.assertEqual((state_dir / 'signing.json').stat().st_mode & 0o777, 0o600)
                    guest.run(['/usr/bin/security', 'lock-keychain', str(expected_keychain)])
                    state = guest.unlock_state()
                    guest.verify_signing(state, set(fingerprints))
                    repeated = guest.restore_state(bundle)
                    self.assertEqual(result, repeated)
                    self.assertTrue(any(p.read_bytes() == b'old-fixture-key' for p in (state_dir / 'backups' / bundle['id']).iterdir() if p.suffix != '.path'))
                    self.assertEqual((state_dir / 'source-environment').read_text(), 'export FIXTURE_SETTING=ready\n')
            finally:
                for path in reversed(created):
                    if path.exists():
                        subprocess.run(['/usr/bin/security', 'delete-keychain', str(path)], capture_output=True)


if __name__ == '__main__':
    unittest.main()
