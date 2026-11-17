# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Common Commands

### Automated Build Script
Build host: `brendan@mac-host` (target host OS: macOS 27.2).

Use the provided script to build both Divvun images and push to registry. See DEVELOPMENT.md for prerequisites and the macOS 27 host requirement when building vanilla from IPSW:

```bash
# Build with defaults (golden-gate, Xcode 27.2)
./build-divvun-images.sh

# Build with specific versions  
./build-divvun-images.sh sequoia 16.4

# Clean up build artifacts after building
./cleanup-build-artifacts.sh

# Dry run cleanup (see what would be deleted)
./cleanup-build-artifacts.sh golden-gate true
```

### Manual Building
Build base macOS image:
```bash
packer build -var macos_version=sonoma templates/base.pkr.hcl
```

Build custom base macOS image (with Buildkite, Tailscale, Rust):
```bash
# First clone vanilla and disable SIP
tart pull ghcr.io/cirruslabs/macos-golden-gate-vanilla:latest
tart clone ghcr.io/cirruslabs/macos-golden-gate-vanilla:latest golden-gate-base-divvun
packer build -var vm_name=golden-gate-base-divvun templates/disable-sip-with-username.pkr.hcl
# Then build base
packer build -var vm_name=golden-gate-base-divvun templates/base-divvun.pkr.hcl
```

Build Xcode image:
```bash
packer build -var macos_version=sonoma -var xcode_version="[15.4]" templates/xcode.pkr.hcl
```

Build custom Xcode image (streamlined for Buildkite):
```bash
# The template provisions a VM that already exists at its final disk size
tart clone golden-gate-base-divvun golden-gate-xcode-divvun:27.2
tart set golden-gate-xcode-divvun:27.2 --disk-size 140
packer build -var macos_version=golden-gate -var xcode_version='["27.2"]' -var xcode_components='["MetalToolchain"]' templates/xcode-divvun.pkr.hcl
```

Build vanilla macOS image:
```bash
packer build templates/vanilla-sonoma.pkr.hcl
```

Disable SIP on an existing image:
```bash
packer build -var vm_name=sonoma-vanilla templates/disable-sip.pkr.hcl
```

### Image Management
Pull base image before building:
```bash
tart pull ghcr.io/cirruslabs/macos-sonoma-base:latest
```

Push built image to registry:
```bash
tart push macos-sonoma-xcode:15.4 ghcr.io/cirruslabs/macos-sonoma-xcode:15.4
```

Initialize Packer templates:
```bash
packer init templates/xcode.pkr.hcl
```

## Architecture Overview

This repository contains Packer templates for building macOS virtual machine images using the Tart virtualization platform. The architecture follows a layered approach:

### Image Hierarchy
1. **Vanilla Images** (`templates/vanilla-*.pkr.hcl`) - Clean macOS installations with minimal configuration
2. **Base Images** (`templates/base.pkr.hcl`) - Built on vanilla, adds Homebrew, development tools, GitHub Actions runner
3. **Custom Base Images** (`templates/base-divvun.pkr.hcl`) - Built on vanilla (with SIP disabled), adds Buildkite agent, Tailscale, Rust, custom tools
4. **Xcode Images** (`templates/xcode.pkr.hcl`) - Built on base, adds Xcode, iOS development tools, Flutter, Android SDK
5. **Custom Xcode Images** (`templates/xcode-divvun.pkr.hcl`) - Built on base-divvun, adds Xcode with minimal mobile tooling

### Key Components
- **templates/** - Packer HCL configuration files for different image types
- **data/** - Configuration files and plists copied into images
- **scripts/** - Shell scripts executed during image provisioning
- **ansible/** - Ansible playbooks for additional configuration
- **.ci/** - Cirrus CI configuration for automated builds

### Template Variables
Core variables used across templates:
- `macos_version` - Target macOS version (golden-gate, tahoe, sequoia, sonoma, etc.)
- `xcode_version` - List of Xcode versions to install
- `disk_size` - VM disk size in GB (default: 140 for Xcode images)
- `disk_free_mb` - Minimum free space required (default: 15000MB)

### Provisioning Flow
Xcode images follow this provisioning sequence:
1. Install and configure development tools (brew, openjdk, Android SDK)
2. Install xcodes tool and Xcode versions from XIP files
3. Download iOS platforms and simulator runtimes
4. Install mobile development tools (fastlane, cocoapods, flutter)
5. Configure GitHub Actions compatibility
6. Generate setup information for runtime introspection

### CI/CD Integration
- Automated builds triggered by GitHub releases for new Xcode versions
- Monthly rebuilds for base images with updated packages
- Images published to GitHub Container Registry
- Supports both beta and stable Xcode releases

## Custom Templates

### Custom Base Image Features
- **Buildkite Agent**: Installed but requires manual token configuration post-deployment
- **Dynamic Hostname**: Auto-generates vm-builder-XXXXX pattern
- **SSH Keys**: Ed25519 key pair generated in ~/.ssh/
- **Tailscale**: CLI and daemon installed, requires manual connection
- **Rust**: Installed with iOS targets (x86_64-apple-darwin, aarch64-apple-ios, aarch64-apple-ios-sim)
- **Custom Tools**: just, cmake, bison, flex, python@3.11, boost, pytorch
- **Buildkite Job Tools**: node, pnpm, uv, coreutils, ninja, autoconf, imagemagick, zstd, sentry-cli, cocoapods, Rust nightly with rust-src, and rcodesign, kbdgen and outto in /usr/local

### Custom Xcode Image Features
- **Streamlined**: No Android SDK, Flutter, or mobile CI tools
- **Buildkite Ready**: Based on custom base with Buildkite agent
- **Essential iOS Tools**: xcbeautify, swiftformat, swiftlint, swiftgen
- **Multiple Xcode Support**: Can install multiple Xcode versions like standard template

### Post-Deployment Configuration

Use `scripts/builder-state.py` to export the old deployed builder to an encrypted
age backup, then restore it onto the replacement deployed VM. This transfers
signing identities, SSH keys, the Buildkite token/configuration and custom hooks.
See DEVELOPMENT.md for the complete commands and backup-key location.

```bash
python3 scripts/builder-state.py export --vm builder-tahoe-1 --output "$HOME/BuilderBackups/builder-tahoe-1.age"
python3 scripts/builder-state.py restore --vm builder-golden-gate-1 --input "$HOME/BuilderBackups/builder-tahoe-1.age" --start-agent
```

Restore credentials only onto deployed clones, after image publishing. Packer
installs public Buildkite defaults and the managed environment hook. Ansible uses
the Homebrew configuration path and accepts `buildkite_environment` for additional
variables, with secrets supplied through Vault or the encrypted migration.

## Persistent Storage

Custom images include `/opt/d` as a symlink for optional persistent storage. To use it:

```bash
tart run --dir=persistent:/path/to/storage vm-name
```

The `/opt/d` path inside the VM will map directly to your host storage directory.

## Buildkite Agent Configuration

After creating a custom VM, configure Buildkite using Ansible:

### Prerequisites
Install Ansible with required dependencies:
```bash
pip install ansible paramiko
```

### Configure Buildkite Agent
```bash
# Get VM IP address
VM_IP=$(tart ip vm-name)

# Configure Buildkite agent
ansible-playbook -i "$VM_IP," -u admin --ask-pass \
  --ask-vault-pass -e @/private/path/builder-vars.vault.yml \
  -e buildkite_tags="macos,xcode16,arm64" \
  ansible/playbook-buildkite-setup.yml
```

### Available Configuration Options
- `buildkite_token`: Your Buildkite agent token (required)
- `buildkite_tags`: Comma-separated list of tags for the agent
- `buildkite_priority`: Agent priority (default: "0")
- `buildkite_name`: Agent name pattern (default: "%hostname-%spawn")
- `buildkite_build_path`: Build directory path

### Complete VM Setup Workflow
```bash
# 1. Start VM with persistent storage
tart run --dir=persistent:~/vm-persistent golden-gate-xcode-divvun:27.2 &

# 2. Wait for VM to boot
sleep 30

# 3. Configure Buildkite
VM_IP=$(tart ip golden-gate-xcode-divvun:27.2)
ansible-playbook -i "$VM_IP," -u admin --ask-pass \
  --ask-vault-pass -e @/private/path/builder-vars.vault.yml \
  -e buildkite_tags="macos,xcode27,arm64" \
  ansible/playbook-buildkite-setup.yml
```

## System Updates

### Update Everything
To update all packages and tools in the VM:

```bash
# Get VM IP
VM_IP=$(tart ip vm-name)

# Run comprehensive update
ansible-playbook -i "$VM_IP," -u admin --ask-pass \
  ansible/playbook-update-all.yml
```

This updates:
- **Homebrew** and all packages
- **Rust toolchain** and cargo packages  
- **Ruby gems** (system and installed)
- **Deno** (via Homebrew)
- **macOS system updates**
- **Python pip packages** (if installed)

The playbook shows version information for all updated tools at the end.