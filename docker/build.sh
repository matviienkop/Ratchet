#!/usr/bin/env bash
# Build the Ratchet bot image: an official Hermes Agent release plus this repository.
#
#   docker/build.sh                       newest Hermes release, tag ratchet-bot:...
#   HERMES_VERSION=v0.23.4 docker/build.sh
#   IMAGE=ghcr.io/acme/ratchet-bot docker/build.sh --push
#
# Environment:
#   HERMES_VERSION   Hermes release tag, or "latest" (default) for the newest GitHub release
#   HERMES_IMAGE     base image repository (default nousresearch/hermes-agent)
#   HERMES_REPO      GitHub repository whose releases name the version (default NousResearch/hermes-agent)
#   IMAGE            name of the image to build (default ratchet-bot)
#   CLAUDE_CODE_VERSION  npm version of Claude Code, empty to leave it out (default latest)
# Options:
#   --push       push both tags after the smoke test
#   --no-smoke   skip docker/smoke.sh
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
push=0 smoke=1
for arg in "$@"; do
    case "$arg" in
        --push) push=1 ;;
        --no-smoke) smoke=0 ;;
        -h|--help) sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

hermes_image=${HERMES_IMAGE:-nousresearch/hermes-agent}
hermes_repo=${HERMES_REPO:-NousResearch/hermes-agent}
hermes_version=${HERMES_VERSION:-latest}
image=${IMAGE:-ratchet-bot}

if [ "$hermes_version" = latest ]; then
    hermes_version=$(curl -fsSL --retry 3 "https://api.github.com/repos/${hermes_repo}/releases/latest" |
        python3 -c 'import json, sys; print(json.load(sys.stdin)["tag_name"])')
    echo "Newest Hermes release: ${hermes_version}"
fi
if ! docker manifest inspect "${hermes_image}:${hermes_version}" >/dev/null 2>&1; then
    echo "No image ${hermes_image}:${hermes_version}. Set HERMES_VERSION to a published release." >&2
    exit 1
fi

revision=$(git -C "$root" rev-parse --short=12 HEAD)
if [ -n "$(git -C "$root" status --porcelain)" ]; then
    revision="${revision}-dirty"
fi
tag="${image}:hermes-${hermes_version#v}-ratchet-${revision}"

docker build --pull \
    --file "$root/docker/Dockerfile" \
    --build-arg "HERMES_IMAGE=${hermes_image}" \
    --build-arg "HERMES_VERSION=${hermes_version}" \
    --build-arg "RATCHET_REVISION=${revision}" \
    --build-arg "CLAUDE_CODE_VERSION=${CLAUDE_CODE_VERSION-latest}" \
    --tag "$tag" --tag "${image}:latest" \
    "$root"

if [ "$smoke" = 1 ]; then
    "$root/docker/smoke.sh" "$tag" "$hermes_version"
fi
if [ "$push" = 1 ]; then
    docker push "$tag"
    docker push "${image}:latest"
fi
echo "Built ${tag}"
