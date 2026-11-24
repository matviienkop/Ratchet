#!/bin/sh
# Dedicated Hermes instance with active Codex and Claude subscriptions.
set -eu
source_root=${1:?Pass the Hermes source checkout}
script_dir=$(dirname "$0")
python3 "$script_dir/availability-fallback.py" "$source_root"
for lane in default coder reviewer; do
    if [ "$lane" = default ]; then
        peer=claude-sonnet-5-5
        set --
    else
        peer=claude-opus-5-5
        set -- -p "$lane"
    fi
    hermes "$@" config set fallback_providers "[{\"provider\":\"claude-subscription-directsdk-experimental\",\"model\":\"$peer\"}]"
done
