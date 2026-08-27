#!/bin/bash -e
# Fork-CI twin of dmwm/WMCore-Jenkins WMCore-Test-Base/setup-env.sh.
# Same workspace tree and sed fixes. Jenkins copies real secrets and grid
# certs; this repo stays secret-free and writes dummy credentials instead.

WORKSPACE="${HOST_MOUNT_DIR:?HOST_MOUNT_DIR must be set}"
: "${COUCH_TAG:?}" "${MDB_TAG:?}" "${WMA_TAG:?}"

# clean workspace (same as setup-env.sh)
rm -rf "$WORKSPACE"/admin "$WORKSPACE"/certs "$WORKSPACE"/artifacts
rm -rf "$WORKSPACE"/srv/{couchdb,mariadb,wmagent}

# make directories (same as setup-env.sh)
mkdir -p "$WORKSPACE"/admin/wmagent "$WORKSPACE"/admin/mariadb "$WORKSPACE"/admin/couchdb "$WORKSPACE"/certs "$WORKSPACE"/artifacts "$WORKSPACE"/home
mkdir -p "$WORKSPACE"/srv/couchdb/"${COUCH_TAG}"/install
mkdir -p "$WORKSPACE"/srv/couchdb/"${COUCH_TAG}"/logs
mkdir -p "$WORKSPACE"/srv/couchdb/"${COUCH_TAG}"/state
mkdir -p "$WORKSPACE"/srv/couchdb/"${COUCH_TAG}"/config
mkdir -p "$WORKSPACE"/srv/mariadb/"${MDB_TAG}"/install/database
mkdir -p "$WORKSPACE"/srv/mariadb/"${MDB_TAG}"/logs
mkdir -p "$WORKSPACE"/srv/wmagent/"${WMA_TAG}"/install
mkdir -p "$WORKSPACE"/srv/wmagent/"${WMA_TAG}"/logs
mkdir -p "$WORKSPACE"/srv/wmagent/"${WMA_TAG}"/state
mkdir -p "$WORKSPACE"/srv/wmagent/"${WMA_TAG}"/config

# secrets: dummy values, same variable names as the Jenkins template.
cat > "$WORKSPACE"/admin/wmagent/WMAgent.secrets <<'EOF'
MYSQL_USER=unittestagent
MYSQL_PASS=passwd
COUCH_USER=unittestagent
COUCH_PASS=passwd
COUCH_PORT=6994
COUCH_HOST=THISHOSTNAME
RUCIO_ACCOUNT=wma_test
RUCIO_HOST=http://cms-rucio.cern.ch
RUCIO_AUTH=https://cms-rucio-auth.cern.ch
GRAFANA_TOKEN=test_fake_token
EOF

# wmagent-mariadb checks the container's OS user against MDB_ROOT,
# so write the real runner username here.
cat > "$WORKSPACE"/admin/mariadb/MariaDB.secrets <<EOF
MDB_ROOT=$(id -un)
MDB_ROOTPASS=passwd
MDB_USER=unittestagent
MDB_PASS=passwd
EOF

# MariaDB data on RAM via /dev/shm, owned by the runner user (docker's own
# tmpfs is root-owned and fails the image's chown). Parallel stacks each get
# their own dir via WMCI_MDB_DATA_DIR, so rm -rf stays scoped to that stack.
MDB_DATA_DIR="${WMCI_MDB_DATA_DIR:-/dev/shm/wmci-mariadb-$(id -u)}"
rm -rf "$MDB_DATA_DIR"
mkdir -p "$MDB_DATA_DIR"
mkdir -p "$WORKSPACE"/tmp

# containers resolve the runner uid through these generated files (the VM
# account is from sssd, not in host /etc/passwd).
if [ -n "${WMCORE_JENKINS_HOST_DIR:-}" ] && [ -f "$WMCORE_JENKINS_HOST_DIR/WMCore-PR-test/setup-users.sh" ]; then
    MY_USER="$(id -un)" MY_GROUP="$(id -g)" HOST_MOUNT_DIR="$WORKSPACE" \
        bash "$WMCORE_JENKINS_HOST_DIR/WMCore-PR-test/setup-users.sh"
fi

# Real credentials, if present on the runner VM: a directory outside the
# repo provides the grid certificate pair and rucio account name. Repo stays
# secret-free; if the directory is missing, CI falls back to a dummy pair.
CI_SECRETS_DIR="${WMCI_SECRETS_DIR:-/data/vkhlaisu/ci-secrets}"
if [ -f "$CI_SECRETS_DIR/usercert.pem" ] && [ -f "$CI_SECRETS_DIR/userkey.pem" ]; then
    echo "Using grid certificate pair from $CI_SECRETS_DIR"
    cp "$CI_SECRETS_DIR/usercert.pem" "$WORKSPACE"/certs/servicecert.pem
    cp "$CI_SECRETS_DIR/userkey.pem"  "$WORKSPACE"/certs/servicekey.pem
    if [ -s "$CI_SECRETS_DIR/rucio_account.txt" ]; then
        sed -i "s/^RUCIO_ACCOUNT=.*/RUCIO_ACCOUNT=$(head -1 "$CI_SECRETS_DIR/rucio_account.txt")/" \
            "$WORKSPACE"/admin/wmagent/WMAgent.secrets
    fi
else
    # self-signed dummy certificate pair instead of real grid certs
    echo "No CI secrets at $CI_SECRETS_DIR; generating a self-signed pair"
    openssl req -x509 -newkey rsa:2048 -nodes -days 30 \
        -subj "/CN=wmcore-fork-ci" \
        -keyout "$WORKSPACE"/certs/servicekey.pem \
        -out "$WORKSPACE"/certs/servicecert.pem 2>/dev/null
fi
chmod 600 "$WORKSPACE"/certs/servicecert.pem
chmod 400 "$WORKSPACE"/certs/servicekey.pem

# fix some incorrect information in secrets files (same seds as setup-env.sh)
# change to correct CouchDB port
sed -i 's/6994/5984/g' "$WORKSPACE"/admin/wmagent/WMAgent.secrets
sed -i 's/THISHOSTNAME/localhost/g' "$WORKSPACE"/admin/wmagent/WMAgent.secrets
# replace mysql with mariadb
sed -i 's/MYSQL/MDB/g' "$WORKSPACE"/admin/wmagent/WMAgent.secrets

echo "Workspace prepared at $WORKSPACE"
ls -l "$WORKSPACE"
