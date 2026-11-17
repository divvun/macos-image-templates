#!/bin/bash

# Configuration
MACOS_VERSION="${1:-golden-gate}"
DRY_RUN="${2:-false}"

echo "🧹 Cleaning up Divvun build artifacts..."
echo "macOS Version: $MACOS_VERSION"
echo "Dry run: $DRY_RUN"
echo

# Function to run command or just show it
run_or_show() {
    echo "Would run: $1"
    if [[ "$DRY_RUN" != "true" ]]; then
        eval "$1" || echo "  ⚠️  Command failed (continuing...)"
    fi
}

# Clean up intermediate VMs
echo "=== Cleaning up intermediate VMs ==="
INTERMEDIATE_VMS=("${MACOS_VERSION}-base-divvun")
# Dynamically find xcode-divvun VMs for this macOS version
while IFS= read -r vm; do
    INTERMEDIATE_VMS+=("$vm")
done < <(tart list --source local --quiet 2>/dev/null | grep "^${MACOS_VERSION}-xcode-divvun:" || true)

for vm in "${INTERMEDIATE_VMS[@]}"; do
    if tart list --source local --quiet | grep -Fxq -- "$vm"; then
        echo "🗑️  Deleting VM: $vm"
        run_or_show "tart delete '$vm'"
    else
        echo "✅ VM not found (already clean): $vm"
    fi
done

# Clean up Xcode cache if it exists
echo
echo "=== Cleaning up Xcode cache ==="
if [[ -d "$HOME/XcodesCache" ]]; then
    echo "📦 XcodesCache directory found:"
    du -sh "$HOME/XcodesCache"
    echo "⚠️  Not automatically cleaning XcodesCache - contains expensive downloads!"
    echo "    Manual cleanup: rm ~/XcodesCache/Xcode_*.xip"
else
    echo "✅ No XcodesCache directory found"
fi

# Clean up Packer cache
echo
echo "=== Cleaning up Packer cache ==="
PACKER_CACHE_DIR="$HOME/.cache/packer"
if [[ -d "$PACKER_CACHE_DIR" ]]; then
    echo "📁 Packer cache found:"
    du -sh "$PACKER_CACHE_DIR" 2>/dev/null || echo "  (empty or inaccessible)"
    run_or_show "rm -rf '$PACKER_CACHE_DIR'"
else
    echo "✅ No Packer cache found"
fi

# Clean up Tart cache
echo
echo "=== Cleaning up Tart cache ==="
TART_CACHE_DIR="$HOME/.tart/cache"
if [[ -d "$TART_CACHE_DIR" ]]; then
    echo "📁 Tart cache found:"
    du -sh "$TART_CACHE_DIR" 2>/dev/null || echo "  (empty or inaccessible)"
    echo "📋 Cached images:"
    ls -la "$TART_CACHE_DIR" 2>/dev/null || echo "  (unable to list)"
    run_or_show "rm -rf '$TART_CACHE_DIR'"
else
    echo "✅ No Tart cache found"
fi

# Clean up temporary files
echo
echo "=== Cleaning up temporary files ==="
TEMP_PATTERNS=(
    "/tmp/packer-*"
    "/tmp/tart-*" 
    "$HOME/.tart/vms/*.tmp"
)

for pattern in "${TEMP_PATTERNS[@]}"; do
    if ls $pattern 2>/dev/null | head -1 > /dev/null; then
        echo "🧽 Cleaning: $pattern"
        run_or_show "rm -rf $pattern"
    else
        echo "✅ Clean: $pattern"
    fi
done

# Show disk usage
echo
echo "=== Current disk usage ==="
echo "Tart VMs directory:"
du -sh "$HOME/.tart/vms" 2>/dev/null || echo "  (not found)"

echo
if [[ "$DRY_RUN" == "true" ]]; then
    echo "🔍 This was a dry run. Run without 'true' parameter to actually clean:"
    echo "   ./cleanup-build-artifacts.sh $MACOS_VERSION"
else
    echo "✅ Cleanup complete!"
fi