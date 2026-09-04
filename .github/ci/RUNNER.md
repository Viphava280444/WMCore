# Setting up a new self-hosted runner

Everything the CI executes lives in this repository - the test scripts are
vendored under `.github/ci/scripts/` and maintained there (provenance in
its README). A new VM provides only docker, the runner itself, and
optionally real grid credentials. One script prepares all of it.

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

The same secrets dir also holds `cmst0.keytab`, which only the Tier-0 SSH
check (`t0-ssh-check.yml`) reads; the test workflows ignore it.

## Paths and multiple runners

The scripts come from the repo checkout, so no script path exists on the
VM at all. The only VM path the workflows know is the optional secrets
dir; a runner with a different one sets the repo Actions variable
`WMCI_SECRETS_DIR` (Settings > Secrets and variables > Actions >
Variables). That one variable moves the whole dir - the grid pair, the
rucio account file and the Tier-0 keytab all have to live under the new
path. Variables are repo-wide, so a fleet of runners should use the same
path convention on every VM.

A runner that has an extra library directory to put on the test
containers' `PYTHONPATH` (the previous CI used one for
`FWCore.ParameterSet.Config`) names it in the Actions variable
`WMCI_EXTRA_PYTHONPATH`. Unset, `PYTHONPATH` is left alone. It must be
the same on baseline and PR runs, which repo-wide variables guarantee.

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
