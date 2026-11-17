#!/bin/bash

source ~/.zprofile

set -euo pipefail

# The version the Linux image in divvun-actions pins (docker/versions.ts).
RCODESIGN_VERSION=0.29.0
TARGET=aarch64-apple-darwin

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

sudo mkdir -p /usr/local/bin /usr/local/libexec

# Prints the download URL of the first asset on a GitHub release whose name
# matches a pattern. The rolling releases put a build timestamp in the name.
release_asset() {
  curl -fsSL "https://api.github.com/repos/$1/releases/tags/$2" |
    jq -er --arg pattern "$3" \
      'first(.assets[] | select(.name | test($pattern)) | .browser_download_url)'
}

curl -fsSL "https://github.com/indygreg/apple-platform-rs/releases/download/apple-codesign%2F$RCODESIGN_VERSION/apple-codesign-$RCODESIGN_VERSION-macos-universal.tar.gz" |
  tar -xz -C "$tmp" --strip-components 1
sudo install -m 0755 "$tmp/rcodesign" /usr/local/bin/rcodesign

# kbdgen is published as a bare binary.
curl -fsSL "$(release_asset divvun/kbdgen v4-latest "^kbdgen_${TARGET}_")" -o "$tmp/kbdgen"
sudo install -m 0755 "$tmp/kbdgen" /usr/local/bin/kbdgen

# outto looks for its helpers in ../libexec, so its bin and libexec stay siblings.
curl -fsSL "$(release_asset divvun/outto dev-latest "^outto_${TARGET}_.*[.]tgz\$")" |
  sudo tar -xz --no-same-owner -C /usr/local --strip-components 1

/usr/local/bin/rcodesign --version
/usr/local/bin/kbdgen --version
test -x /usr/local/bin/outto
test -x /usr/local/libexec/outto-gui
