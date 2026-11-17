#!/bin/bash
# Managed by divvun-builder.
# Buildkite sources this hook for every job. Keep sourced secrets out of xtrace.
_divvun_restore_xtrace=false
case "$-" in *x*) _divvun_restore_xtrace=true; set +x ;; esac
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:$PATH"
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
