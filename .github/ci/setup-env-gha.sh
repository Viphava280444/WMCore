#!/bin/bash -e
# Fork-CI twin of dmwm/WMCore-Jenkins WMCore-Test-Base/setup-env.sh (commit b203d873).
# Same workspace tree and the same sed fixes.
# The only difference: the Jenkins agent copies real secret templates and grid
# certificates from /home/cmsbld/.globus. This CI keeps the fork secret-free on
# purpose, so it writes dummy credentials (values based on
# test/deploy/WMAgent_unittest.secrets) and generates a self-signed certificate.

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

# secrets: dummy values, same variable names the Jenkins template provides.
# Base: test/deploy/WMAgent_unittest.secrets (this repo) plus MYSQL_PASS,
# which the scripts read after the MYSQL->MDB rename below.
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

# the wmagent-mariadb image checks that the OS user running the container
# matches MDB_ROOT from this file, so write the real runner username
cat > "$WORKSPACE"/admin/mariadb/MariaDB.secrets <<EOF
MDB_ROOT=$(id -un)
MDB_ROOTPASS=passwd
MDB_USER=unittestagent
MDB_PASS=passwd
EOF

# MariaDB data on RAM via /dev/shm: docker's own tmpfs mounts are root-owned
# and the image's chown as the runner user fails on them, so bind a host RAM
# directory owned by the runner user instead
MDB_SHM_DIR="/dev/shm/wmci-mariadb-$(id -u)"
rm -rf "$MDB_SHM_DIR"
mkdir -p "$MDB_SHM_DIR"

# containers resolve the runner uid through these generated files (the VM
# account comes from sssd and is absent from the host /etc/passwd); this
# reuses WMCore-Jenkins/WMCore-PR-test/setup-users.sh unchanged
if [ -n "${WMCORE_JENKINS_HOST_DIR:-}" ] && [ -f "$WMCORE_JENKINS_HOST_DIR/WMCore-PR-test/setup-users.sh" ]; then
    MY_USER="$(id -un)" MY_GROUP="$(id -g)" HOST_MOUNT_DIR="$WORKSPACE" \
        bash "$WMCORE_JENKINS_HOST_DIR/WMCore-PR-test/setup-users.sh"
fi

# real credentials, when present on the runner VM (fork twin of the Jenkins
# agent copying grid certificates from /home/cmsbld/.globus): a VM-local
# directory OUTSIDE the repository provides the user's grid certificate pair
# and rucio account name. The repository stays secret-free - when the
# directory is absent, CI falls back to the self-signed dummy pair, and the
# baseline comparison still forgives the resulting environment failures.
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
    # self-signed certificate pair instead of real grid certs (dummy, 30 days)
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
