# Setting up a new self-hosted runner

Everything the CI executes lives in this repository, except two things a
new VM provides once: a clone of the private `dmwm/WMCore-Jenkins` repo
(the unchanged Jenkins-era test scripts) and, optionally, real grid
credentials. One script prepares all of it.

## Quick start

On the new VM, as the future runner user:

```bash
curl -sfO https://raw.githubusercontent.com/<owner>/WMCore/master/.github/ci/setup-runner.sh
bash setup-runner.sh --repo <owner>/WMCore --token <registration-token>
```

The registration token comes from the repo page
Settings > Actions > Runners > "New self-hosted runner". The script is
idempotent: when it stops on a missing prerequisite, fix that and re-run.

## What the VM must have

| Piece | Why | Notes |
|---|---|---|
| docker + compose v2 | test containers | runner user in `docker` group |
| /dev/shm, 4 GB+ | MariaDB data on RAM | per-stack dirs, auto-cleaned |
| /etc/grid-security/certificates | CA bundle mount | standard on CERN VMs |
| WMCore-Jenkins clone | Jenkins-era scripts | private repo, script clones it |
| python3, git, curl, openssl | glue | stock on AlmaLinux |

No NAT or firewall change is needed: containers run on a private bridge
and reach the internet through a unix-socket proxy served by the job
(`ci-proxy.py`). No cvmfs. Ports 3306/5984 stay free because every stack
runs in its own network namespace.

## Credentialed mode (optional)

Without credentials the CI generates a self-signed certificate pair; the
extra environment failures appear on both baseline and PR sides and are
forgiven by the comparison. To run with real credentials, place the grid
certificate pair and rucio account name into the secrets dir by hand -
the script writes a README there naming the exact files. The rucio
account's DN registrations (e.g. `wma_test`) are admin requests, not
files.

## Paths and multiple runners

The workflows default to the original VM's paths. A runner with different
paths sets two repo Actions variables (Settings > Secrets and variables >
Actions > Variables): `WMCORE_JENKINS_HOST_DIR` and `WMCI_SECRETS_DIR`.
These are repo-wide, so a fleet of runners should use the same path
convention on every VM.

## Known traps

- After `usermod -aG docker`, an already-running runner service keeps its
  old groups and fails silently. Restart the service and verify:
  `grep Groups /proc/$(pgrep -f Runner.Listener | head -1)/status`
  must list the docker group's gid.
- The workspace persists between runs on a self-hosted runner. The
  workflows clean what they consume; do not park files in the runner
  work directory.
- Runner labels must include `self-hosted, Linux, X64` (the defaults).

## Verify the setup

Dispatch the "WMCore baseline" workflow once. Green with 14/14 slices and
~1650 tests in the summary means the runner is fully working.
