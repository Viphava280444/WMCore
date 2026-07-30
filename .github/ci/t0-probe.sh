#!/bin/bash
# Read-only probe that runs ON a Tier-0 host as the cmst0 service account.
#
# It is fed to the remote shell by .github/workflows/t0-ssh-check.yml with
#   ssh ... cmst0@<host> 'bash -s' < .github/ci/t0-probe.sh
# so it is never installed on the target machines.
#
# CONTRACT: this script must never change anything on the host. Every command
# below only reads. It prints one "key=value" line per fact; the workflow parses
# those lines to build its summary table. Keep the key names stable.
set -u

# cmst0's HOME is on AFS and is not readable without a Kerberos token, so docker
# would print a warning trying to load its config from there. Point it at a
# throwaway directory instead.
DOCKER_CONFIG="$(mktemp -d)"
export DOCKER_CONFIG
trap 'rm -rf "$DOCKER_CONFIG"' EXIT

echo "who=$(id -un)"
echo "host=$(hostname -f)"
echo "uptime=$(uptime -p 2>/dev/null || echo unknown)"
echo "disk=$(df -h /data 2>/dev/null | tail -1 | awk '{print $5" used, "$4" free"}')"
echo "condor=$(systemctl is-active condor 2>&1 | head -1)"

# The Tier-0 workload on these hosts runs as containers, not as a WMAgent
# checkout: vocms0500 carries the production comp-ops stack and vocms05012 the
# -smoke copy of it. Report what is up rather than guessing at a fixed name.
names=$(docker ps --format '{{.Names}}' 2>/dev/null | sort | tr '\n' ' ')
echo "containers=${names:-none}"
echo "container_count=$(docker ps -q 2>/dev/null | wc -l | tr -d ' ')"
echo "unhealthy=$(docker ps --filter health=unhealthy --format '{{.Names}}' 2>/dev/null | tr '\n' ' ')"
echo "procs=$(ps -u cmst0 --no-headers 2>/dev/null | wc -l | tr -d ' ')"
