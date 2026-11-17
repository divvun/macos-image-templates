#!/bin/bash
set -e

# Configuration
MACOS_VERSION="${1:-golden-gate}"
XCODE_VERSION="${2:-27.2}"
BASE_VM="$MACOS_VERSION-base-divvun"
XCODE_VM="$MACOS_VERSION-xcode-divvun:$XCODE_VERSION"
XCODE_DISK_SIZE=140

echo "Building Divvun macOS images..."
echo "macOS Version: $MACOS_VERSION"
echo "Xcode Version: $XCODE_VERSION"
echo

# VERIFY_ONLY=1 checks an image that is already built, without rebuilding it.
if [[ "${VERIFY_ONLY:-}" != "1" ]]; then
  # Check host dependencies
  for cmd in packer tart xcodes sshpass; do
    if ! command -v "$cmd" &>/dev/null; then
      echo "ERROR: '$cmd' is required but not installed"
      echo "  brew install $cmd"
      exit 1
    fi
  done

  # Ensure Xcode XIP is available with the expected name
  XIP_PATH="$HOME/XcodesCache/Xcode_$XCODE_VERSION.xip"
  if [[ -f "$XIP_PATH" ]]; then
    echo "=== Xcode $XCODE_VERSION XIP already cached ==="
  else
    mkdir -p "$HOME/XcodesCache"
    # Check if xcodes downloaded it with its own naming (Xcode-VERSION+BUILD.xip)
    EXISTING=$(ls "$HOME/XcodesCache"/Xcode-"$XCODE_VERSION"*.xip 2>/dev/null | head -1)
    if [[ -z "$EXISTING" ]]; then
      echo "=== Downloading Xcode $XCODE_VERSION ==="
      xcodes download "$XCODE_VERSION" --directory "$HOME/XcodesCache"
      EXISTING=$(ls "$HOME/XcodesCache"/Xcode-"$XCODE_VERSION"*.xip 2>/dev/null | head -1)
    fi
    if [[ -n "$EXISTING" ]]; then
      ln -s "$EXISTING" "$XIP_PATH"
    else
      echo "ERROR: Could not find Xcode $XCODE_VERSION XIP in ~/XcodesCache/"
      ls "$HOME/XcodesCache/"*.xip 2>/dev/null || echo "  (no .xip files found)"
      exit 1
    fi
  fi

  # Determine SIP disable template
  if [[ "$MACOS_VERSION" == "sonoma" ]]; then
    SIP_TEMPLATE="disable-sip.pkr.hcl"
  else
    SIP_TEMPLATE="disable-sip-with-username.pkr.hcl"
  fi

  # Step 1: Build base image (skip if it already exists)
  if tart list --source local --quiet | grep -Fxq -- "$BASE_VM"; then
    echo "=== Base image '$BASE_VM' already exists, skipping ==="
  else
    echo "=== Pulling vanilla image ==="
    tart pull "ghcr.io/cirruslabs/macos-$MACOS_VERSION-vanilla:latest"
    tart clone "ghcr.io/cirruslabs/macos-$MACOS_VERSION-vanilla:latest" "$BASE_VM"

    echo "=== Disabling SIP ==="
    packer init "templates/$SIP_TEMPLATE"
    packer build -var vm_name="$BASE_VM" "templates/$SIP_TEMPLATE"

    echo "=== Building base-divvun image ==="
    packer init templates/base-divvun.pkr.hcl
    packer build -var vm_name="$BASE_VM" templates/base-divvun.pkr.hcl
  fi

  # Step 2: Build Xcode image
  echo "=== Building xcode-divvun image ==="
  if tart list --source local --quiet | grep -Fxq -- "$XCODE_VM"; then
    echo "ERROR: '$XCODE_VM' already exists. Delete it first: tart delete $XCODE_VM"
    exit 1
  fi

  # The disk gets its final size before the first boot, where the guest daemon
  # expands APFS into it. Packer provisions the VM as it finds it.
  tart clone "$BASE_VM" "$XCODE_VM"
  tart set "$XCODE_VM" --disk-size "$XCODE_DISK_SIZE"

  packer init templates/xcode-divvun.pkr.hcl
  if ! packer build \
    -var macos_version="$MACOS_VERSION" \
    -var xcode_version="[\"$XCODE_VERSION\"]" \
    -var xcode_components='["MetalToolchain"]' \
    -var disk_size="$XCODE_DISK_SIZE" \
    templates/xcode-divvun.pkr.hcl; then
    tart delete "$XCODE_VM" || true
    exit 1
  fi
fi

# Step 3: Verify the image
echo "=== Verifying image ==="
tart run "$XCODE_VM" --no-graphics &
TART_PID=$!

# Wait for VM to become accessible
echo "Waiting for VM to boot..."
for i in $(seq 1 60); do
  VM_IP=$(tart ip "$XCODE_VM" 2>/dev/null || true)
  if [[ -n "$VM_IP" ]]; then
    break
  fi
  sleep 5
done

if [[ -z "$VM_IP" ]]; then
  echo "ERROR: VM did not become accessible"
  kill "$TART_PID" 2>/dev/null || true
  exit 1
fi

echo "VM IP: $VM_IP"
echo "Running verification checks..."

# Run checks via SSH (sshpass needed for non-interactive)
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10"
SSH_CMD="sshpass -p admin ssh $SSH_OPTS admin@$VM_IP"

FAILED=0
verify() {
  local desc="$1"
  shift
  local output
  if output=$($SSH_CMD "$@" 2>&1); then
    echo "  OK: $desc"
  else
    echo "  FAIL: $desc"
    echo "$output" | tail -5 | sed 's/^/        /'
    FAILED=1
  fi
}

if [[ "$MACOS_VERSION" == "golden-gate" ]]; then
  verify "macOS Golden Gate (27)" "test \$(sw_vers -productVersion | cut -d. -f1) = 27"
fi
verify "SIP disabled" "csrutil status | grep -q disabled"
verify "Xcode installed" "source ~/.zprofile && xcodes installed | grep -q '$XCODE_VERSION'"
verify "swift works" "source ~/.zprofile && swift --version"
verify "Rust installed" "source ~/.cargo/env && rustup show | grep -q aarch64-apple-ios"
verify "Rust nightly with rust-src" "source ~/.cargo/env && rustup component list --toolchain nightly --installed | grep -q rust-src"
verify "Buildkite agent installed" "source ~/.zprofile && buildkite-agent --version"
verify "Node, pnpm and uv" "source ~/.zprofile && node --version && pnpm --version && uv --version"
verify "rcodesign, kbdgen and outto" "/usr/local/bin/rcodesign --version && /usr/local/bin/kbdgen --version && test -x /usr/local/bin/outto"
verify "cmake, ninja and timeout where jobs expect them" "test -x /opt/homebrew/bin/cmake && test -x /opt/homebrew/bin/ninja && test -x /opt/homebrew/opt/coreutils/libexec/gnubin/timeout"
verify "autoconf, convert, zstd and sentry-cli" "source ~/.zprofile && autoconf --version && convert -version && zstd --version && sentry-cli --version"
verify "fastlane, CocoaPods and xcodegen" "source ~/.zprofile && fastlane --version && pod --version && xcodegen --version"
verify "iPhone 18 Pro simulator" "source ~/.zprofile && xcrun simctl list devices available | grep -q 'iPhone 18 Pro ('"
verify "Tailscale installed" "source ~/.zprofile && which tailscale"
verify "/usr/local/bin owned by admin" "test \$(stat -f %Su /usr/local/bin) = admin"
verify "SSH keys exist" "test -f ~/.ssh/id_ed25519"

# Shut down the VM
$SSH_CMD "sudo shutdown -h now" 2>/dev/null || true
sleep 5
kill "$TART_PID" 2>/dev/null || true
wait "$TART_PID" 2>/dev/null || true

if [[ "$FAILED" -eq 1 ]]; then
  echo
  echo "ERROR: Some verification checks failed"
  exit 1
fi

echo
echo "All checks passed!"

# Step 4: Push
if [[ "${SKIP_PUSH:-}" == "1" ]]; then
  echo "SKIP_PUSH=1: not pushing. To publish:"
  echo "  tart push $XCODE_VM ghcr.io/divvun/$XCODE_VM"
  exit 0
fi

echo "=== Pushing xcode-divvun image to ghcr.io/divvun ==="
tart push "$XCODE_VM" "ghcr.io/divvun/$XCODE_VM"

echo "Build complete! Image pushed to ghcr.io/divvun/$XCODE_VM"
