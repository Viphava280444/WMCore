#!/usr/bin/env bash
# Injection test, phase 2 placeholder: put a PR on the testbed agent and take
# it off again.
#
#   agent.sh patch <diff-file>    put the PR diff on the testbed agent with
#                                 bin/patchComponent.sh (the diff on stdin)
#   agent.sh unpatch <tag>        undo the patch recorded under <tag>
#                                 (recreate the agent container)
#
# Stage M1a builds only the interface. Nothing here touches a machine:
#   - WMCI_INJECT_PATCH unset or not 'on': print what phase 2 would do, exit 0
#   - WMCI_INJECT_PATCH=on: refuse, exit 3 (phase 2 is not built yet)
# Usage errors exit 2. No ssh, no sudo, no network, no file writes.
# The workflow does not call this script in M1a.

set -euo pipefail

usage() {
    echo "usage: agent.sh patch <diff-file> | agent.sh unpatch <tag>" >&2
    exit 2
}

# print one line per changed file, taken from the 'diff --git a/X b/X'
# headers only (never the diff content); each line is indented and stripped
# of control characters, so no line can start a runner command
diff_file_names() {
    sed -n 's|^diff --git a/\(.*\) b/.*$|\1|p' < "$1" \
        | LC_ALL=C tr -cd '[:print:]\n' \
        | sed 's|^|  - |'
}

[ "$#" -ge 1 ] || usage
verb=$1
shift

case "$verb" in
    patch)
        [ "$#" -eq 1 ] || usage
        diff_file=$1
        if [ ! -f "$diff_file" ] || [ ! -r "$diff_file" ]; then
            echo "agent.sh: '$diff_file' is not a readable regular file" >&2
            exit 2
        fi
        ;;
    unpatch)
        [ "$#" -eq 1 ] || usage
        tag=$1
        if ! [[ "$tag" =~ ^[A-Za-z0-9._-]{1,64}$ ]]; then
            echo "agent.sh: the tag must match ^[A-Za-z0-9._-]{1,64}\$" >&2
            exit 2
        fi
        ;;
    *)
        usage
        ;;
esac

patch_mode=${WMCI_INJECT_PATCH:-off}
if [ "$patch_mode" != "on" ]; then
    echo "agent.sh: WMCI_INJECT_PATCH is '${patch_mode}', not 'on' - nothing done."
    if [ "$verb" = "patch" ]; then
        echo "would run on the testbed agent: bin/patchComponent.sh < $(basename -- "$diff_file")"
        echo "files in the diff:"
        diff_file_names "$diff_file"
    else
        echo "would undo patch ${tag} on the testbed agent (recreate the container)"
    fi
    exit 0
fi

echo "agent.sh: phase 2 is not built in M1a - refusing."
exit 3
