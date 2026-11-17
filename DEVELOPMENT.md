## Building Divvun Images

The target build host is `brendan@mac-host` running macOS 27.2. Run the commands
below from a checkout of this repository on that host after its OS upgrade finishes.

The default build uses macOS 27 **Golden Gate** and Xcode 27.2:

```bash
./build-divvun-images.sh
# Equivalent explicit versions:
./build-divvun-images.sh golden-gate 27.2
```

Install Tart from its maintained Homebrew tap (`brew install openai/tools/tart`),
or update it with `brew upgrade openai/tools/tart`. Packer, `xcodes`, and `sshpass`
are also required. Xcode downloads may require an Apple developer account.

The script pulls `ghcr.io/cirruslabs/macos-golden-gate-vanilla:latest`, disables SIP,
provisions the Divvun base image, and installs Xcode from `~/XcodesCache/Xcode_27.2.xip`
(downloading it if needed). After verifying the guest OS and development tools, it
pushes `ghcr.io/divvun/golden-gate-xcode-divvun:27.2`. Set `SKIP_PUSH=1` to build and
verify without publishing, for instance over SSH, where `tart` cannot read the
registry login from the keychain.

`VERIFY_ONLY=1` reruns the checks against an image that is already built. The
Xcode stage clones the base image and sets its disk size before Packer starts,
so delete a leftover `golden-gate-xcode-divvun:27.2` before rebuilding.

The existing Tahoe images use separate names. To rebuild that version explicitly:

```bash
./build-divvun-images.sh tahoe 26.4
```

Preview cleanup of the Golden Gate build artifacts with:

```bash
./cleanup-build-artifacts.sh golden-gate true
```

## Migrating signing keys and Buildkite configuration

Run these commands on `mac-host` from the repository checkout, with the source
and destination VMs running. Install `age` on the controller once; the guests need
only Python 3, the macOS `security` tools, and Buildkite already installed.

```bash
brew install age
install -d -m 700 "$HOME/BuilderBackups"

# Capture the working builder once, before replacing it.
python3 scripts/builder-state.py export \
  --vm builder-tahoe-1 \
  --output "$HOME/BuilderBackups/builder-tahoe-1.age"

# Restore onto a deployed clone after building the new image.
python3 scripts/builder-state.py restore \
  --vm builder-golden-gate-1 \
  --input "$HOME/BuilderBackups/builder-tahoe-1.age" \
  --start-agent

# Repeatable signing check, including signing and verifying a temporary binary.
python3 scripts/builder-state.py verify --vm builder-golden-gate-1
```

The first export creates an age identity at
`~/Library/Application Support/Divvun Builder/backup.agekey` (mode 0600).
Keep a secure copy of that identity separately from the encrypted backups. An
existing backup is never overwritten; choose a new filename for a new snapshot.
To run from another machine, use `--host admin@VM_IP --jump brendan@mac-host`
instead of `--vm`. SSH uses the usual keys or password authentication.

The encrypted backup contains:

* PKCS#12 identities from keychains containing valid signing identities, with
  expected certificate fingerprints.
* SSH `id_*` identity files (including their public keys).
* The Homebrew Buildkite configuration, including its agent token, queue, worker
  count, paths, and all custom hooks.
* `~/.local/bin/env` and `~/.config/divvun-builder/environment`, when present.

Source signing keychains must be unlocked and allow export. If necessary, pass
`--unlock-file /private/path/builder-unlock.json`: a mode-0600 JSON map of full
source keychain paths to their passwords. It is sent over SSH stdin. An export
fails if no valid identities can be found or a required key export fails.
Non-exportable keys still require replacement or their original signing provider.

Restore creates a dedicated keychain, configures access for signing tools, and
checks that every expected identity can sign a binary before installing the agent
configuration. The managed Buildkite hook unlocks that keychain for each job using
a mode-0600 state file. Passwords are passed to `security` over stdin rather than
process arguments. Plaintext bundles stay in memory on the controller; temporary
PKCS#12 files in the guests are removed afterward.

Stop destination agents before restoring. Changed destination files are backed
up under `~/.config/divvun-builder/backups/`; repeating the same restore reuses its
keychain. The original environment hook is retained and sourced by the managed
hook. Omit `--start-agent` to restore and verify without accepting jobs. Apply this
to a deployed VM after image publishing, so credentials stay out of shared images.

### Buildkite environment configuration

Both Packer and Ansible install `data/buildkite-environment.sh`. It provides
Homebrew, Cargo and user-local tools on PATH, loads the original custom hook and
`~/.config/divvun-builder/environment`, then unlocks the managed signing keychain.
It suppresses shell tracing while loading these files.

The Ansible role configures the file Homebrew actually reads:
`/opt/homebrew/etc/buildkite-agent/buildkite-agent.cfg`. Defaults match the existing
builder: `spawn=2`, `tags="queue=macos"`, and Homebrew build/hook/plugin paths.
`buildkite_environment` is a dictionary of additional environment variables;
Ansible writes these to the private environment file with mode 0600 and disables
logging/diffs for secret-bearing tasks. Supply secrets through an Ansible Vault
vars file, or use the encrypted restore above to carry over existing settings.

```bash
ansible-galaxy collection install -r ansible/requirements.yml
ansible-playbook -i "VM_IP," -u admin --ask-pass \
  --ask-vault-pass -e @/private/path/builder-vars.vault.yml \
  ansible/playbook-buildkite-setup.yml
```

Set `buildkite_start_agent: false` to configure without starting the service.
The migration restore never exports Tailscale node identity or unrelated keychain
password items; reconnect Tailscale through its normal enrollment process.

## Building Golden Gate from an IPSW

The Divvun build above uses a prebuilt vanilla image. To create a vanilla image
from Apple's restore image instead, use the
[upstream Golden Gate provisioning flow](https://github.com/cirruslabs/macos-image-templates/blob/main/templates/vanilla-golden-gate.pkr.hcl):

```bash
packer init templates/vanilla-golden-gate.pkr.hcl
packer build templates/vanilla-golden-gate.pkr.hcl
```

This template requires **Tart 2.33.0 or later and macOS 27 or later on the host**
for `--provisioning-opts`. It uses an ASIF disk and macOS 27.0 (26A428), enables
automatic login and SSH during first-boot provisioning, and enables Screen Sharing
through System Settings. Upgrading Tart alone does not satisfy the host OS
requirement for this IPSW build.

## Building Vanilla Image

To build `macos-sonoma-vanilla`:

```bash
packer build templates/vanilla-sonoma.pkr.hcl
```

Optionally, SIP can be disabled for each image by running the following commands:

```bash
packer build -var vm_name=sonoma-vanilla templates/disable-sip.pkr.hcl
```

## Building Base Image

```bash
packer build -var vm_name=sonoma-vanilla templates/base.pkr.hcl
```

## Building Xcode Image

```bash
packer build -var macos_version=sonoma -var xcode_version="[15.4]" templates/xcode.pkr.hcl
```