#! /usr/bin/env python

import os
import sys
import time

from github import Github


try:
    gh = Github(os.environ['DMWMBOT_TOKEN'])
except KeyError:
    print('DMWMBOT_TOKEN not defined. Not updating PR')

codeRepo = os.environ.get('CODE_REPO', 'WMCore')
teamName = os.environ.get('WMCORE_REPO', 'dmwm')
repoName = f'{teamName}/{codeRepo}'

issueID = None

if 'PR_NUMBER' in os.environ:
    issueID = os.environ['PR_NUMBER']
    mode = 'PR'
elif 'TargetIssueID' in os.environ:
    issueID = os.environ['TargetIssueID']
    mode = 'Daily'

print(f"Looking for {repoName} issue {issueID}")

repo = gh.get_repo(repoName)
issue = repo.get_issue(int(issueID))
reportURL = os.environ['BUILD_URL']

# The status must land on the commit the tests actually run on. PR_HEAD_SHA is
# that commit, pinned by the workflow when the run started. Without it, fall
# back to the PR head: the old get_page(0)[-1] returned the 30th commit of a
# PR with more than 30 commits, not its head.
pull = repo.get_pull(int(issueID))
sha = os.environ.get('PR_HEAD_SHA') or pull.head.sha
lastCommit = repo.get_commit(sha)

lastCommit.create_status(
    state='pending',
    target_url=reportURL,
    description=f'Tests started at {time.strftime("%d %b %Y %H:%M GMT")}'
)
