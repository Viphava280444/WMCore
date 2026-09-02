#!/bin/bash -e
# Prepare a new self-hosted runner VM for the WMCore CI.
#
# Run as the future runner user on the VM. Idempotent: safe to re-run.
# Everything the CI executes lives in this repo (the Jenkins-era scripts
# are vendored under .github/ci/scripts); the only hand step left is
# pasting a runner registration token from the repo's Settings page.
#
# Usage:
#   setup-runner.sh --repo <owner/repo> [--token <registration-token>]
#     [--secrets-dir /data/$USER/ci-secrets]
#     [--runner-dir  /data/$USER/actions-runner]

REPO=""
REG_TOKEN=""
SECRETS_DIR="/data/$USER/ci-secrets"
RUNNER_DIR="/data/$USER/actions-runner"

while [ $# -gt 0 ]; do
  case "$1" in
    --repo)        REPO="$2"; shift 2;;
    --token)       REG_TOKEN="$2"; shift 2;;
    --secrets-dir) SECRETS_DIR="$2"; shift 2;;
    --runner-dir)  RUNNER_DIR="$2"; shift 2;;
    *) echo "unknown option: $1"; exit 1;;
  esac
done
[ -n "$REPO" ] || { echo "--repo <owner/repo> is required"; exit 1; }

fail=0
note() { printf '%s\n' "$*"; }
need() { command -v "$1" >/dev/null || { note "MISSING: $1"; fail=1; }; }

note "== 1/5 host prerequisites"
need git; need python3; need openssl; need curl; need docker
if docker compose version >/dev/null 2>&1; then
  note "docker compose: $(docker compose version --short 2>/dev/null)"
else
  note "MISSING: docker compose v2 plugin"; fail=1
fi
shm_kb=$(df -k /dev/shm 2>/dev/null | awk 'NR==2{print $2}')
if [ "${shm_kb:-0}" -lt 4000000 ]; then
  note "WARNING: /dev/shm is ${shm_kb:-0} KB; MariaDB data lives there, 4 GB+ recommended"
fi
if [ ! -d /etc/grid-security/certificates ]; then
  note "WARNING: /etc/grid-security/certificates missing (CERN CA bundle)."
  note "  Credentialed tests need it; on CERN VMs it comes from the standard config."
fi
[ $fail -eq 0 ] || { note "install the missing tools first, then re-run"; exit 1; }

note "== 2/5 docker access"
if docker ps >/dev/null 2>&1; then
  note "docker works for user $USER"
else
  note "user $USER cannot talk to docker. As root, run:"
  note "  usermod -aG docker $USER"
  note "then LOG OUT AND BACK IN and re-run this script."
  note "(If the runner service already exists, restart it too - a running"
  note " service keeps its old group list and fails silently.)"
  exit 1
fi

note "== 3/5 optional credentials dir at $SECRETS_DIR"
if [ -f "$SECRETS_DIR/usercert.pem" ]; then
  note "grid certificate pair present - credentialed mode"
else
  mkdir -p "$SECRETS_DIR"; chmod 700 "$SECRETS_DIR"
  cat > "$SECRETS_DIR/README" <<'EOF'
Optional. To run the tests with real grid credentials, place here BY HAND:
  usercert.pem       (mode 644)  your grid certificate
  userkey.pem        (mode 400)  its key, passphrase-free
  rucio_account.txt  one line: the rucio account name
Never commit these anywhere. Without them the CI generates a self-signed
pair and the extra environment failures are forgiven by the baseline diff.
EOF
  note "skeleton created (dummy-cert mode until you add real files - see its README)"
fi

note "== 4/5 actions runner at $RUNNER_DIR"
if [ -f "$RUNNER_DIR/.runner" ]; then
  note "runner already configured"
else
  mkdir -p "$RUNNER_DIR"; cd "$RUNNER_DIR"
  if [ ! -f config.sh ]; then
    url=$(curl -sf https://api.github.com/repos/actions/runner/releases/latest \
      | python3 -c 'import json,sys; a=[x["browser_download_url"] for x in json.load(sys.stdin)["assets"] if "linux-x64-2" in x["name"]]; print(a[0])')
    note "downloading $url"
    curl -sfL "$url" | tar xz
  fi
  [ -n "$REG_TOKEN" ] || { note "get a registration token from https://github.com/$REPO/settings/actions/runners/new and re-run with --token"; exit 1; }
  ./config.sh --url "https://github.com/$REPO" --token "$REG_TOKEN" --unattended
  note "installing the systemd service (needs sudo):"
  sudo ./svc.sh install "$USER" && sudo ./svc.sh start
fi

note "== 5/5 verification"
pid=$(pgrep -f Runner.Listener | head -1 || true)
if [ -n "$pid" ]; then
  dgid=$(getent group docker | cut -d: -f3)
  if grep -q "\b$dgid\b" "/proc/$pid/status"; then
    note "runner process is in the docker group - OK"
  else
    note "WARNING: runner process NOT in docker group; restart the service:"
    note "  sudo $RUNNER_DIR/svc.sh stop && sudo $RUNNER_DIR/svc.sh start"
  fi
else
  note "runner process not found - start the service and re-check"
fi
note ""
note "Done. If the secrets dir differs from the workflow default, set the"
note "repo Actions variable WMCI_SECRETS_DIR=$SECRETS_DIR"
note "Then dispatch the 'WMCore baseline' workflow once and watch it go green."
