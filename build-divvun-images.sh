#!/bin/bash
set -e

# Configuration
MACOS_VERSION="${1:-sonoma}"
XCODE_VERSION="${2:-16.0}"

echo "Building Divvun macOS images..."
echo "macOS Version: $MACOS_VERSION"
echo "Xcode Version: $XCODE_VERSION"

# Build base image
echo "=== Building base-divvun image ==="
packer init templates/base-divvun.pkr.hcl
packer build -var macos_version="$MACOS_VERSION" templates/base-divvun.pkr.hcl

# Build Xcode image
echo "=== Building xcode-divvun image ==="
packer init templates/xcode-divvun.pkr.hcl
packer build -var macos_version="$MACOS_VERSION" -var xcode_version="[\"$XCODE_VERSION\"]" templates/xcode-divvun.pkr.hcl

# Push Xcode image to registry
echo "=== Pushing xcode-divvun image to ghcr.io/divvun ==="
IMAGE_NAME="$MACOS_VERSION-xcode-divvun:$XCODE_VERSION"
tart push "$IMAGE_NAME" "ghcr.io/divvun/macos-$IMAGE_NAME"

echo "✅ Build complete! Image pushed to ghcr.io/divvun/macos-$IMAGE_NAME"