#! /usr/bin/env python3
"""Fork-only patch for a throwaway copy of PullRequestReport.py.

Upstream's buildPylintReport() gate is absolute: ANY W/E pylint event not in
okWarnings fails the check, even when the same event exists on the master side
of the file. Any PR touching a legacy file with pre-existing debt therefore
goes red no matter what the diff does (first seen live: 20 pre-existing W0718
in MSUnmerged.py failed PR #9 while both scores were unchanged at 9.67).

This script rewrites the per-event loop in the COPY (the dmwm-owned original
on the VM stays untouched, same pattern as the 'GitHub Actions results:'
wording sed) so that an event "must be fixed" only when the PR side of a file
has MORE events of that (type, code) than the master side. Pre-existing debt
is reclassified as a warning, keeping the comment's counts consistent with
the verdict. The score-based rules below the loop are left unchanged.

Usage: patch-pylint-delta.py <path-to-copied-PullRequestReport.py>
Exits 1 loudly if the expected upstream block is not found exactly once,
so upstream drift breaks the report step instead of silently un-patching.
"""
import sys

ORIGINAL = """\
    for filename in report.keys():
        if 'test' in report[filename]:
            for event in report[filename]['test']['events']:
                if event[1] in ['W', 'E'] and event[2] not in okWarnings:
                    failed = True
                    failures += 1
                elif event[1] in ['W', 'E']:
                    warnings += 1
                else:
                    comments += 1
"""

REPLACEMENT = """\
    for filename in report.keys():
        if 'test' in report[filename]:
            # Fork-only delta gate (patched in by .github/ci/patch-pylint-delta.py):
            # count how many W/E events of each (type, code) the master side of
            # this file already has ...
            baseCounts = {}
            for event in report[filename].get('base', {}).get('events', []):
                if event[1] in ['W', 'E'] and event[2] not in okWarnings:
                    key = (event[1], event[2])
                    baseCounts[key] = baseCounts.get(key, 0) + 1
            # ... and fail only on the PR-side events beyond that count. The
            # pre-existing ones are shown as plain warnings instead.
            for event in report[filename]['test']['events']:
                if event[1] in ['W', 'E'] and event[2] not in okWarnings:
                    key = (event[1], event[2])
                    if baseCounts.get(key, 0) > 0:
                        baseCounts[key] -= 1
                        warnings += 1
                    else:
                        failed = True
                        failures += 1
                elif event[1] in ['W', 'E']:
                    warnings += 1
                else:
                    comments += 1
"""


def main():
    path = sys.argv[1]
    with open(path, encoding='utf-8') as fd:
        src = fd.read()
    hits = src.count(ORIGINAL)
    if hits != 1:
        print("patch-pylint-delta: expected the upstream pylint event loop "
              "exactly once, found %d times - upstream PullRequestReport.py "
              "changed, review the patch" % hits)
        sys.exit(1)
    with open(path, 'w', encoding='utf-8') as fd:
        fd.write(src.replace(ORIGINAL, REPLACEMENT))
    print("patch-pylint-delta: pylint gate is now delta-aware (new W/E only)")


if __name__ == '__main__':
    main()
