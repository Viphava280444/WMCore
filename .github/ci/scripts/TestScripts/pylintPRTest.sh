#!/usr/bin/env bash

# No 'set -e' on purpose: pycodestyle and pylint exit non-zero whenever they
# find something, and this script is expected to keep going and report it.
# pipefail only makes a failing left-hand side of a pipe visible.
set -o pipefail

WORKDIR=/home/cmsbld

if [ -z "$PR_NUMBER" -o -z "$TARGET_BRANCH" ]; then
  echo "Not all necessary environment variables set: PR_NUMBER, TARGET_BRANCH"
  exit 1
fi

echo "Executing Pylint for PR ID $PR_NUMBER and target branch $TARGET_BRANCH"

# Setup the environment
echo "Sourcing a python3 unittest environment"
source $WORKDIR/TestScripts/env_unittest.sh
JSON_FILENAME=pylintpy3Report.json
PEP8_FILENAME=pep8py3.txt

pushd $WORKDIR/WMCore
export PYTHONPATH=`pwd`/test/python:`pwd`/src/python:$PYTHONPATH

# Figure out the one commit we are interested in and what happens to the repo if we were to merge it
git config remote.origin.url "https://github.com/${WMCORE_ORG:-dmwm}/WMCore.git"
git fetch origin pull/${PR_NUMBER}/merge:PR_MERGE
# A closed or conflicting PR has no merge ref. Without this check the rev-parse
# below prints nothing, COMMIT stays empty and every git command after it acts
# on the target branch, so the whole run lints the wrong tree and looks clean.
git rev-parse --verify -q "PR_MERGE^{commit}" >/dev/null || { echo "ERROR: no merge ref for PR ${PR_NUMBER}: the PR is closed or has merge conflicts"; exit 1; }
# PR_HEAD_SHA is the commit the maintainer reviewed when the run was requested.
# If the branch moved since, this run would lint unreviewed code, so stop.
if [ -n "${PR_HEAD_SHA:-}" ]; then
  got=$(git rev-parse "PR_MERGE^2")
  [ "$got" = "$PR_HEAD_SHA" ] || { echo "ERROR: PR head moved since the tests were requested (expected ${PR_HEAD_SHA}, merge ref has ${got}); request the tests again"; exit 1; }
fi
# split assignment: `export COMMIT=$(...)` always returns 0, so a failing
# rev-parse would go unnoticed even under 'set -e'
COMMIT=$(git rev-parse "PR_MERGE^{commit}") || exit 1
export COMMIT
git checkout ${TARGET_BRANCH}
git pull

# Which python files changed? CI tooling under .github is not product
# code, so the style gate skips it.
git diff --name-only  ${TARGET_BRANCH}..${COMMIT} | grep -v '^\.github/' > allChangedFiles.txt
$WORKDIR/ContainerScripts/IdentifyPythonFiles.py allChangedFiles.txt > changedFiles.txt

echo "Printing Pylint version"
pylint --version

# Get pylint report for master
git checkout -f $TARGET_BRANCH
echo "{}" > pylintReport.json
echo "*** Running Pylint on the changed files against the $TARGET_BRANCH"
while read name; do
  pylint --evaluation='10.0 - ((float(5 * error + warning) / statement) * 10)'  --rcfile standards/.pylintrc --msg-template='{path}:{line}: [{msg_id}({symbol}), {obj}] {msg}'  $name > pylint.out || true
  $WORKDIR/ContainerScripts/AggregatePylint.py base
done <changedFiles.txt

# Get pylint report for the tip of our branch
git checkout -f $COMMIT
echo "*** Running Pylint on the changed files against the feature branch"
while read name; do
  pylint --evaluation='10.0 - ((float(5 * error + warning) / statement) * 10)'  --rcfile standards/.pylintrc  --msg-template='{path}:{line}: [{msg_id}({symbol}), {obj}] {msg}'  $name  > pylint.out || true
  $WORKDIR/ContainerScripts/AggregatePylint.py test
done <changedFiles.txt

# Save the artifacts to a directory shared by the container and the node
# update file if it's a python3 pylint job
mv pylintReport.json ${JSON_FILENAME} || true
cp *.json $WORKDIR/artifacts/

touch NOTHING # If changedFiles.txt is empty, this will keep it from parsing the whole directory tree
pycodestyle NOTHING `< changedFiles.txt` > ${PEP8_FILENAME}
cp ${PEP8_FILENAME} $WORKDIR/artifacts/

# Completion marker. An empty changedFiles.txt is legitimate (a PR that touches
# no python file), so the report cannot tell "nothing to lint" from "the lint
# run died half way" by looking at the report file alone. This marker is only
# written when the script ran to the end; the report job refuses to publish a
# Py3 Pylint verdict without it.
echo ok > $WORKDIR/artifacts/lint-status.txt

popd
