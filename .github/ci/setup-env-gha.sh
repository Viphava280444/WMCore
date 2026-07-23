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

cat > "$WORKSPACE"/admin/mariadb/MariaDB.secrets <<'EOF'
MDB_USER=unittestagent
MDB_PASS=passwd
EOF

# self-signed certificate pair instead of real grid certs (dummy, 30 days)
openssl req -x509 -newkey rsa:2048 -nodes -days 30 \
    -subj "/CN=wmcore-fork-ci" \
    -keyout "$WORKSPACE"/certs/servicekey.pem \
    -out "$WORKSPACE"/certs/servicecert.pem 2>/dev/null
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
