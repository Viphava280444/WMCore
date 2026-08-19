# Hosted CI scaffolding (zero CERN resources)

Files in this directory serve only `.github/workflows/wmcore-hosted-ci.yml`,
which runs on GitHub-hosted `ubuntu-latest` runners. The workflow is ADDITIVE
and advisory: `wmcore-pr-test.yml` on vocms047 remains the authority for the
Jenkins-contract statuses and the PR comment. This workflow has
`permissions: contents: read` only, so it structurally cannot post them.

## Why each piece exists

- **sitecustomize.py** — mysqlclient (MySQLdb) has no manylinux wheels, so it
  cannot be pip-installed on a plain runner. PyMySQL registers itself as
  MySQLdb at interpreter start. This keeps the `DATABASE` URL in the bare
  `mysql://` shape that `env_unittest.sh` uses and leaves
  `WMQuality/TestInit.py` unpatched. It only works because the workflow puts
  this directory first on PYTHONPATH; the "sanity" step in the workflow fails
  fast with a readable message if that ever breaks.
- **requirements-hosted.txt** — pip-only pins. gfal2-python is excluded the
  same way dmwm's `pypi_build_and_images.yaml` excludes it on ubuntu-latest.
- **`client_flag=65536` in the DATABASE URL** — `WMInit.setSchemaFromModules`
  feeds a whole wmcoredb `.sql` file as ONE statement, which needs MariaDB's
  `CLIENT.MULTI_STATEMENTS` (= 65536). Without it, pymysql raises
  `ProgrammingError 1064` at `CREATE TABLE wmbs_location_state`.
- **couchdb host port must stay 5984** — the container's replicator reconnects
  to the URL it is handed, so remapping the host port breaks replication tests.

## Test scope and quarantine list (v1)

Scope: `test/python/WMCore_t/WMBS_t` + `test/python/WMCore_t/Database_t`.
Measured twice on fresh `mariadb:10.6` + `couchdb:3.3`: 162 passed, 2 skipped,
0 failed. Exclusions, each with its reason:

- `WMBS_t/JobSplitting_t` and `Database_t/CouchUtils_t.py` — import
  `WMQuality.TestInitCouchApp`, which imports `couchapp`; couchapp is not pip
  installable on py3.12 (its dependency restkit needs the removed `imp` module).
- `Database_t/CMSCouch_t.py` — 4 failures: `testGetSchedulerDocs` /
  `testGetSchedulerJobs` are master test bugs (they index a list as a dict);
  2 more are intra-file test-order pollution (pass in isolation).
- `Database_t/RotatingDatabase_t.py` — `testArchive` / `testCycle` fail even
  with COUCHURL set; needs triage.
- `WMBS_t/File_t.py::FileTest::testParentageByMergeJob` — deterministic
  failure, reproduces in isolation; the assertion at `File_t.py:1039` needs
  triage (master test bug vs mariadb 10.6 / wmcoredb behavior).

If you fix one of these upstream, remove it from the list in the workflow —
nothing re-checks the list automatically.
