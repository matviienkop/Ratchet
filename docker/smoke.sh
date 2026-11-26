#!/usr/bin/env bash
# Start the image on an empty volume, check what the boot hook installed, restart the
# container on the same volume and check that the state is still there.
#
#   docker/smoke.sh IMAGE [HERMES_VERSION]
set -euo pipefail

image=${1:?usage: docker/smoke.sh IMAGE [HERMES_VERSION]}
expected=${2:-}
name="ratchet-smoke-$$"
volume="ratchet-smoke-$$"

cleanup() {
    docker rm -f "$name" >/dev/null 2>&1 || true
    docker volume rm "$volume" >/dev/null 2>&1 || true
}
trap cleanup EXIT

in_container() { docker exec -u hermes -e HOME=/opt/data -w /opt/data "$name" sh -ec "$1"; }

start() {
    docker run -d --name "$name" -v "$volume:/opt/data" "$image" sleep 900 >/dev/null
    for _ in $(seq 1 90); do
        if docker logs "$name" 2>&1 | grep -q '\[ratchet\] revision .* installed'; then
            return 0
        fi
        if [ "$(docker inspect -f '{{.State.Running}}' "$name")" != true ]; then
            break
        fi
        sleep 2
    done
    docker logs "$name" 2>&1 | tail -40 >&2
    echo "The boot hook did not finish." >&2
    return 1
}

check() {
    in_container '
        mkdir -p /opt/data/.ai-dev
        test -f /opt/data/.ai-dev/policy.json ||
            printf "%s\n" "{\"defaults\": {\"checks\": [{\"name\": \"unit\", \"argv\": [\"true\"]}]}}" > /opt/data/.ai-dev/policy.json
        hermes --version | sed -n 1p
        gh --version | sed -n 1p
        claude --version | sed -n 1p
        python3 - <<PY
import json, re, subprocess
version = subprocess.run(["gh", "--version"], capture_output=True, text=True, check=True).stdout
major, minor = map(int, re.search(r"gh version (\d+)\.(\d+)", version).groups())
assert (major, minor) >= (2, 48), version
doctor = json.loads(subprocess.run(["ai-dev-card", "doctor"], capture_output=True, text=True).stdout)
assert doctor["problems"] == [], doctor["problems"]
PY
        for home in /opt/data /opt/data/profiles/coder /opt/data/profiles/reviewer; do
            test -f "$home/plugins/ai-dev-gate/plugin.yaml"
            test -d "$home/skills/software-development"
        done
        test -f /opt/data/skills/software-development/ai-dev-planner/SKILL.md
        test -f /opt/data/profiles/coder/skills/software-development/ai-dev-card/SKILL.md
        for profile in "" "-p coder" "-p reviewer"; do
            hermes $profile plugins list 2>/dev/null | grep -i "ai-dev-gate" | grep -qi "enabled"
        done
        test "$(hermes config get kanban.auto_decompose 2>/dev/null | tr -d "[:space:]" | tr A-Z a-z)" = false
    '
    for service in ratchet-notifications ratchet-recovery; do
        docker exec "$name" /command/s6-svstat "/run/service/$service" | grep -q '^up'
    done
}

start
if [ -n "$expected" ]; then
    in_container 'hermes --version' | grep -q "${expected#v}"
fi
check
in_container '
    hermes kanban boards create smoke --name Smoke >/dev/null
    hermes kanban --board smoke create "Persistence marker" >/dev/null
    printf marker > /opt/data/.ai-dev/smoke-marker
'

docker rm -f "$name" >/dev/null
start
check
in_container '
    hermes kanban --board smoke list | grep -q "Persistence marker"
    test "$(cat /opt/data/.ai-dev/smoke-marker)" = marker
'
echo "Smoke test passed for ${image}"
