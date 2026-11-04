#!/bin/bash
# Read-only probe that runs on a Tier-0 host as cmst0.
# Piped in by .github/workflows/t0-ssh-check.yml via ssh; never installed on the host.
#
# Must never change anything on the host. Prints "key=value" lines;
# the workflow parses them, so keep the key names stable.
set -u

# cmst0's HOME is on AFS, unreadable without a Kerberos token.
# Point docker config at a throwaway dir to avoid a warning.
DOCKER_CONFIG="$(mktemp -d)"
export DOCKER_CONFIG
trap 'rm -rf "$DOCKER_CONFIG"' EXIT

echo "who=$(id -un)"
echo "host=$(hostname -f)"
echo "uptime=$(uptime -p 2>/dev/null || echo unknown)"
echo "disk=$(df -h /data 2>/dev/null | tail -1 | awk '{print $5" used, "$4" free"}')"
echo "condor=$(systemctl is-active condor 2>&1 | head -1)"

# These hosts run the Tier-0 workload as containers. Report what is
# up rather than guessing at a fixed name.
names=$(docker ps --format '{{.Names}}' 2>/dev/null | sort | tr '\n' ' ')
echo "containers=${names:-none}"
echo "container_count=$(docker ps -q 2>/dev/null | wc -l | tr -d ' ')"
echo "unhealthy=$(docker ps --filter health=unhealthy --format '{{.Names}}' 2>/dev/null | tr '\n' ' ')"
echo "procs=$(ps -u cmst0 --no-headers 2>/dev/null | wc -l | tr -d ' ')"

# Below is log-only; the workflow ignores it. Proves the Kerberos
# login landed as cmst0 and can read the Tier-0 area.
echo "--- ls -la /data/tier0 (first 20) ---"
ls -la /data/tier0 2>&1 | head -20
echo "--- ls -la /data/dockerMount/srv ---"
ls -la /data/dockerMount/srv 2>&1 | head -10
