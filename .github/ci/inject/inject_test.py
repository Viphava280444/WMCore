#!/usr/bin/env python3
"""
Injection test for a WMCore pull request: stage M1a (fake mode only).

A writer comments "gha injection test please" on a pull request. The workflow
.github/workflows/wmcore-inject.yml calls the sub-commands of this file:

    gate -> check -> files -> inject -> wait -> cleanup -> report

In M1a only the "fake" mode is built: every answer comes from the files in
fixtures/, and nothing is sent to the testbed. The only child process is
test/data/ReqMgr/inject-test-wfs.py with --dryRun, which makes no network call.

Python 3.9, standard library only. Nothing runs at import time.
NOTE: pytest collects "*_test.py", so this file must define no name that
starts with "test" or "Test".
"""

import argparse
import ast
import copy
import json
import os
import posixpath
import pwd
import random
import re
import shutil
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# Constants (spec 2.1). TEAM, SITE, DEADLINE_H, POLL_MIN, HOP_S can be
# overridden with WMCI_INJECT_* variables; configure() validates them in main().
# ---------------------------------------------------------------------------
TESTBED_URL = "https://cmsweb-testbed.cern.ch"   # fixed, no env override (never production)
CAMPAIGN = "GHA_INJ_POC"                         # fixed (busy check + cleanup key)
TEMPLATE = "SC_ProdPsi_small.json"               # fixed
TEMPLATE_MODE = "GHA"                            # -m value; folder requests/GHA/ inside the sandbox
TEAM = "testbed-vocms0263"
SITE = "T2_CH_CERN"
DEADLINE_H = 5.0
POLL_MIN = 15
HOP_S = 1.0
ACQ_ERA = "DMWM_TEST"
PROC_STR = "GHA_INJ"
PROC_VER = "1"
GRACE_POLLS = 2
MIN_SUCCESS = 2
MARKER = "<!-- wmci-inject -->"
STATUS_CONTEXT = "Injection test"
MODES = ["off", "fake", "readonly", "inject-only", "assign"]   # ordered, low to high
BUILT_MODES = {"fake"}                                         # M1a
SCENARIOS = ["pass", "fail", "timeout", "busy", "not-patchable"]

CHILD_TIMEOUT_S = 600
MAX_FILES = 300
MAX_FILE_ROWS = 50
MAX_COMMENT = 60000
STAGES = ["check", "files", "inject", "wait"]
FIXTURE_NAME = "gha_fake_SC_ProdPsi_small_GHA_INJ_PR0_0000000_R0_261006_120000_1234"
OTHER_FAKE_NAME = "gha_fake_other_request"
INJECT_SCRIPT = "test/data/ReqMgr/inject-test-wfs.py"

# request statuses (src/python/WMCore/ReqMgr/DataStructs/RequestStatus.py)
DONE = {"completed", "closed-out", "announced", "normal-archived", "rejected",
        "rejected-archived", "aborted-completed", "aborted-archived"}
COMPLETED_OR_LATER = {"completed", "closed-out", "announced", "normal-archived"}
FAILED_STATUSES = {"failed", "aborted", "aborted-completed", "aborted-archived",
                   "rejected", "rejected-archived"}
WMSTATS_NO_JOB_INFO = {"new", "assignment-approved", "assigned", "staging", "staged",
                       "acquired", "failed", "announced", "aborted", "aborted-completed",
                       "rejected"}
REJECT_FROM = {"new", "assignment-approved", "completed", "failed", "closed-out", "announced"}
ABORT_FROM = {"assigned", "staging", "staged", "acquired", "running-open", "running-closed"}

# Fake timelines (spec 2.5): (status, AgentJobInfo status dict, DBS file count)
_ACQ = [("assigned", {}, 0), ("staging", {}, 0), ("staged", {}, 0), ("acquired", {}, 0)]
FAKE_TIMELINES = {
    "pass": _ACQ + [
        ("running-open", {"queued": {"first": 2}}, 0),
        ("running-open", {"submitted": {"first": 2, "pending": 2}}, 0),
        ("running-open", {"submitted": {"first": 2, "running": 2}}, 0),
        ("running-closed", {"success": 2, "submitted": {"first": 1, "running": 1}}, 0),
        ("completed", {"success": 6}, 0),
        ("completed", {"success": 6}, 2),
    ],
    "fail": _ACQ + [
        ("running-open", {"submitted": {"first": 2, "running": 2}}, 0),
        ("running-open", {"success": 1, "failure": {"exception": 1}}, 0),
    ],
    "timeout": list(_ACQ),
}

# Own-line command regex (spec 2.8), same rule as the "gha test please" trigger.
CMD_RE = re.compile(r"^[^\S\n]*gha injection (test|status|abort) please(?:[^\S\n]|[!.])*$",
                    re.IGNORECASE | re.MULTILINE)
# reqmgr2.py: "Create request '%s' succeeded." ; name characters from Lexicon.requestName
NAME_RE = re.compile(r"Create request '([A-Za-z0-9._-]{1,150})' succeeded\.")
# logger format of inject-test-wfs.py and reqmgr2.py
LOG_LINE_RE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}:(INFO|WARNING|ERROR|CRITICAL):[A-Za-z0-9_-]+: ")

# ---------------------------------------------------------------------------
# Redaction (spec 2.10)
# ---------------------------------------------------------------------------
HIDE_LINE_WORDS = ("Identity files", "cert file", "key file", "X509_USER", "Command line arguments")
SECRET_ENV = ("X509_USER_CERT", "X509_USER_KEY", "X509_USER_PROXY", "WMCI_SECRETS_DIR")
SECRET_KEYS = {"DN", "RequestorDN", "user_dn", "create_by", "last_modified_by",
               "proxy_warning", "down_component_detail"}
# What must never be printed: any path through a "*secrets" folder (the runner's
# credential folder is also hidden by value through WMCI_SECRETS_DIR above), the
# tweaked /tmp json, grid proxy files, certificate and key files, DNs and tokens.
_REDACT_RES = [
    (re.compile(r"[^\s'\"]*/[^\s'\"/]*secrets(?=/|[\s'\"]|$)[^\s'\"]*", re.IGNORECASE), "<secret-path>"),
    (re.compile(r"/tmp/[A-Za-z0-9._-]+\.json"), "<tmp-json>"),
    (re.compile(r"(?:/tmp/)?x509up_u\d+"), "<proxy>"),
    (re.compile(r"\S*user(?:cert|key)\S*"), "<cert>"),
    (re.compile(r"\S+\.(?:pem|p12|key)\b"), "<cert>"),
    (re.compile(r"(?:/(?:DC|C|O|OU|CN|L|ST)=[^/'\",\]\n]+)+"), "<dn>"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "<token>"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "<token>"),
    (re.compile(r"Bearer\s+\S+"), "<token>"),
]


def redact(text):
    """Hide credential lines and replace secret-looking values (pure apart from os.environ)."""
    if text is None:
        return text
    text = str(text)
    lines = []
    for line in text.split("\n"):
        if any(word in line for word in HIDE_LINE_WORDS):
            line = "[line hidden: credentials]"
        lines.append(line)
    text = "\n".join(lines)
    for var in SECRET_ENV:
        value = os.environ.get(var, "")
        if len(value) >= 4:
            text = text.replace(value, "<secret-path>")
    for regex, repl in _REDACT_RES:
        text = regex.sub(repl, text)
    return text


def redact_obj(obj):
    """Redact every string of a JSON-like object and drop the keys that hold DNs or paths."""
    if isinstance(obj, dict):
        return {redact(k): redact_obj(v) for k, v in obj.items() if k not in SECRET_KEYS}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, str):
        return redact(obj)
    return obj


def log_tail(log_text, keep=20):
    """Allowlist of logger lines for the comment (spec 2.10 rule 3)."""
    kept = [line for line in (log_text or "").splitlines() if LOG_LINE_RE.match(line)]
    out = []
    for line in kept[-keep:]:
        line = redact(line).replace("`", "")
        out.append(line[:200])
    return out


# ---------------------------------------------------------------------------
# Errors, configuration, small helpers
# ---------------------------------------------------------------------------
class UsageError(Exception):
    """Bad argument or configuration: exit 2."""


class Refused(Exception):
    """Mode off or not built, or starter outside the allow-list: exit 3."""


def configure(environ):
    """Read and validate the WMCI_INJECT_* overrides; set the module values."""
    global TEAM, SITE, DEADLINE_H, POLL_MIN, HOP_S  # pylint: disable=global-statement
    team = environ.get("WMCI_INJECT_TEAM", "") or "testbed-vocms0263"
    if not re.match(r"^[A-Za-z0-9_-]{1,60}$", team):
        raise UsageError("WMCI_INJECT_TEAM is not a valid team name")
    site = environ.get("WMCI_INJECT_SITE", "") or "T2_CH_CERN"
    if not re.match(r"^T[0-3]_[A-Z]{2}_[A-Za-z0-9_]+$", site):
        raise UsageError("WMCI_INJECT_SITE is not a valid site name")
    try:
        deadline = float(environ.get("WMCI_INJECT_DEADLINE_H", "") or 5)
        poll = int(environ.get("WMCI_INJECT_POLL_MIN", "") or 15)
        hop = float(environ.get("WMCI_INJECT_HOP_S", "") or 1)
    except ValueError:
        raise UsageError("WMCI_INJECT_DEADLINE_H, _POLL_MIN or _HOP_S is not a number") from None
    if not 0 < deadline <= 5:
        raise UsageError("WMCI_INJECT_DEADLINE_H must be above 0 and at most 5")
    if not 1 <= poll <= 60:
        raise UsageError("WMCI_INJECT_POLL_MIN must be 1..60")
    if not 0 <= hop <= 60:
        raise UsageError("WMCI_INJECT_HOP_S must be 0..60")
    TEAM, SITE, DEADLINE_H, POLL_MIN, HOP_S = team, site, deadline, poll, hop


def log(msg):
    """One short redacted line on stderr."""
    sys.stderr.write(redact("inject_test: %s" % msg).replace("\n", " ") + "\n")


def emit(obj):
    """The sub-command result: one JSON object on one stdout line."""
    sys.stdout.write(json.dumps(redact_obj(obj), sort_keys=False) + "\n")


def script_dir():
    return os.path.dirname(os.path.abspath(__file__))


def default_repo_root():
    return os.path.normpath(os.path.join(script_dir(), "..", "..", ".."))


def load_json(path):
    with open(path, encoding="utf-8") as fd:
        return json.load(fd)


def state_path(work):
    return os.path.join(work, "state.json")


def load_state(work):
    try:
        return load_json(state_path(work))
    except FileNotFoundError:
        return None


def save_state(work, state):
    os.makedirs(work, exist_ok=True)
    tmp = state_path(work) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fd:
        json.dump(state, fd, indent=1)
    os.replace(tmp, state_path(work))


def begin_stage(work, state, name):
    state["stage"] = name
    state[name] = {"done": False}
    save_state(work, state)


def end_stage(work, state, name, result):
    state[name].update(result)
    state[name]["done"] = True
    save_state(work, state)


def set_stop(state, stage, verdict, reasons, kind=None):
    state["stop"] = {"stage": stage, "verdict": verdict, "reasons": list(reasons)}
    if kind:
        state["stop"]["kind"] = kind


def refuse(work, state, stage, reason):
    """Exit 3 counts as decided: write the stop (verdict error) and done: true."""
    set_stop(state, stage, "error", [reason], kind="refused")
    end_stage(work, state, stage, {"refused": True, "reasons": [reason]})
    raise Refused(reason)


def skipped(stage, state):
    reason = "stopped at %s (%s)" % (state["stop"]["stage"], state["stop"]["verdict"])
    emit({"skipped": True, "stage": stage, "reason": reason})
    return 0


def plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


# ---------------------------------------------------------------------------
# gate (pure)
# ---------------------------------------------------------------------------
_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def strip_hidden(body):
    """Remove <!-- --> regions and fenced code blocks (``` or ~~~; unclosed runs to the end)."""
    body = re.sub(r"<!--.*?(?:-->|\Z)", "", body or "", flags=re.DOTALL)
    out = []
    fence = None
    for line in body.split("\n"):
        if fence is None:
            match = _FENCE_OPEN.match(line)
            if match:
                fence = match.group(1)
                continue
            out.append(line)
        else:
            close = re.match(r"^ {0,3}(%s{%d,})[^\S\n]*$" % (re.escape(fence[0]), len(fence)), line)
            if close:
                fence = None
    return "\n".join(out)


def parse_command(body):
    """Comment body -> test | status | abort | none. Priority abort > test > status."""
    found = {m.lower() for m in CMD_RE.findall(strip_hidden(body))}
    for cmd in ("abort", "test", "status"):
        if cmd in found:
            return cmd
    return "none"


def split_users(users):
    return [u.strip().lower() for u in (users or "").split(",") if u.strip()]


def user_allowed(login, users):
    return bool(login) and login.strip().lower() in split_users(users)


def ref_allowed(ref, mode):
    if ref == "refs/heads/master":
        return True
    if ref.startswith("refs/heads/ci/injection-") and mode == "fake":
        return True
    return False


def resolve_mode(ceiling, requested):
    """Return (mode, refusal reason or None)."""
    if ceiling not in MODES:
        ceiling = "off"
    if not requested or requested in ("ceiling", ceiling):
        return ceiling, None
    if requested not in MODES:
        return "off", "unknown mode %s" % requested
    if MODES.index(requested) > MODES.index(ceiling):
        return "off", "mode %s is above the ceiling %s" % (requested, ceiling)
    return requested, None


def gate_decide(event, body, login, actor, ref, users, ceiling, requested="", scenario="pass"):
    """The whole gate decision (pure)."""
    reasons = []
    command = "test" if event == "workflow_dispatch" else parse_command(body)
    allowed = user_allowed(login, users) and user_allowed(actor, users)
    mode, why = resolve_mode(ceiling, requested if event == "workflow_dispatch" else "")
    if event != "workflow_dispatch":
        scenario = "pass"
    if command == "none":
        reasons.append("no 'gha injection ... please' line")
    elif command in ("status", "abort"):
        reasons.append("gha injection %s please is not built in M1a (planned for M1b)" % command)
    if not allowed:
        if not user_allowed(login, users):
            reasons.append("%s is not in WMCI_INJECT_USERS" % login)
        if actor != login and not user_allowed(actor, users):
            reasons.append("started by %s, who is not in WMCI_INJECT_USERS" % actor)
    if why:
        reasons.append(why)
    elif mode == "off":
        reasons.append("WMCI_INJECT_MODE is off")
    elif mode not in BUILT_MODES:
        reasons.append("mode %s not built in M1a" % mode)
    if mode != "off" and not ref_allowed(ref, mode):
        reasons.append("mode %s is not allowed from ref %s" % (mode, ref))
    if scenario not in SCENARIOS:
        reasons.append("unknown scenario %s" % scenario)
    go = (command == "test" and allowed and why is None and mode != "off"
          and mode in BUILT_MODES and ref_allowed(ref, mode) and scenario in SCENARIOS)
    return {"command": command, "allowed": allowed, "mode": mode, "scenario": scenario,
            "go": go, "login": login, "reasons": reasons}


def _one_line(value):
    return str(value).replace("\r", " ").replace("\n", " ")


def write_github_output(path, result):
    with open(path, "a", encoding="utf-8") as fd:
        for key in ("command", "allowed", "mode", "scenario", "go", "login", "reasons"):
            value = result[key]
            if isinstance(value, bool):
                value = "true" if value else "false"
            elif isinstance(value, list):
                value = "; ".join(value)
            fd.write("%s=%s\n" % (key, _one_line(value)))


def cmd_gate(args):
    if args.event not in ("issue_comment", "workflow_dispatch"):
        raise UsageError("unknown event")
    body = os.environ.get("WMCI_COMMENT_BODY", "") if args.event == "issue_comment" else ""
    result = gate_decide(args.event, body, args.login, args.actor, args.ref, args.users,
                         args.ceiling, args.requested or "", args.scenario or "pass")
    if args.github_output:
        write_github_output(args.github_output, result)
    emit(result)
    return 0


# ---------------------------------------------------------------------------
# check (pre-flight)
# ---------------------------------------------------------------------------
def agent_ok(rows, team, now):
    """Return (ok, reasons, agents) from the WMStats agentInfo rows (pure)."""
    reasons, agents = [], []
    for row in rows or []:
        info = row.get("value") or {}
        if info.get("agent_team") != team:
            continue
        url = str(info.get("agent_url", "?"))
        down = list(info.get("down_components") or [])
        disks = [str(d.get("mounted", "?")) if isinstance(d, dict) else str(d)
                 for d in (info.get("disk_warning") or [])]
        age = int(now - float(info.get("timestamp") or 0))
        agents.append({"agent_url": url, "team": team, "status": info.get("status"),
                       "drain_mode": bool(info.get("drain_mode")), "down": len(down),
                       "disk": len(disks), "age_s": age})
        if info.get("drain_mode"):
            reasons.append("agent %s is in drain mode" % url)
        if down:
            reasons.append("agent %s has down components: %s" % (url, ", ".join(str(c) for c in down)))
        if disks:
            reasons.append("agent %s has a disk warning on %s" % (url, ", ".join(disks)))
        if info.get("status") in ("down", "error"):
            reasons.append("agent %s status is %s" % (url, info.get("status")))
        if age > 3600:
            reasons.append("agent info is %d min old" % (age // 60))
    if not agents:
        reasons.append("no agent of team %s in WMStats" % team)
    return not reasons, reasons, agents


def busy_requests(answer, own_name=None):
    """Names of active requests in a ReqMgr2 campaign answer (pure)."""
    active = []
    for item in (answer or {}).get("result", []) or []:
        for name, doc in item.items():
            if name == own_name:
                continue
            if (doc or {}).get("RequestStatus") not in DONE:
                active.append(name)
    return active


def _check_args(args):
    checks = [(r"^\d{1,7}$", args.pr, "pr"), (r"^[0-9a-f]{40}$", args.sha, "sha"),
              (r"^\d{1,20}$", args.run, "run"), (r"^https://\S+$", args.run_url, "run-url"),
              (r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}(\[bot\])?$", args.login, "login"),
              (r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}(\[bot\])?$", args.actor, "actor")]
    for regex, value, name in checks:
        if not re.match(regex, value or ""):
            raise UsageError("--%s is not valid" % name)
    if args.mode not in MODES:
        raise UsageError("--mode must be one of %s" % ", ".join(MODES))
    if args.scenario not in SCENARIOS:
        raise UsageError("--scenario must be one of %s" % ", ".join(SCENARIOS))
    fixtures = os.path.abspath(args.fixtures or os.path.join(script_dir(), "fixtures"))
    if not os.path.isdir(fixtures):
        raise UsageError("--fixtures is not a folder")
    return fixtures


def cmd_check(args):
    fixtures = _check_args(args)
    work = args.work
    state = {}
    begin_stage(work, state, "check")
    sha7 = args.sha[:7]
    state["run"] = {"mode": args.mode, "pr": args.pr, "sha": args.sha, "sha7": sha7, "run": args.run,
                    "run_url": args.run_url, "login": args.login, "actor": args.actor,
                    "scenario": args.scenario, "fixtures": fixtures, "started": int(time.time()),
                    "req_string": "GHA_INJ_PR%s_%s_R%s" % (args.pr, sha7, args.run)}
    save_state(work, state)
    if not user_allowed(args.actor, args.users):
        refuse(work, state, "check", "started by %s, who is not in WMCI_INJECT_USERS" % args.actor)
    if args.mode == "off":
        refuse(work, state, "check", "mode is off")
    if args.mode not in BUILT_MODES:
        refuse(work, state, "check", "mode %s not built in M1a" % args.mode)
    clock = FakeClock(time.time())
    source = FakeSource(fixtures, args.scenario, None, clock)
    ok, reasons, agents = agent_ok(source.agent_rows(), TEAM, clock.now())
    active = busy_requests(source.campaign_answer())
    if active:
        reasons.append("testbed busy: %d active %s request(s)" % (len(active), CAMPAIGN))
    go = ok and not active
    result = {"go": go, "reasons": reasons, "agents": agents, "active_requests": active}
    if not ok:
        set_stop(state, "check", "error", reasons, kind="agent")
    elif active:
        set_stop(state, "check", "busy", reasons)
    end_stage(work, state, "check", result)
    emit(result)
    return 0


# ---------------------------------------------------------------------------
# files (classification, from setup_dependencies.py read with ast, never exec)
# ---------------------------------------------------------------------------
SERVICES = {"wmagent": "AGENT", "reqmgr2": "CENTRAL", "reqmon": "CENTRAL", "wmglobalqueue": "CENTRAL",
            "reqmgr2ms": "CENTRAL", "mstransferor": "CENTRAL", "msmonitor": "CENTRAL",
            "msoutput": "CENTRAL", "msrulecleaner": "CENTRAL", "msunmerged": "CENTRAL",
            "mspileup": "CENTRAL"}   # t0agent counts as neither
TOPLEVEL_DIRS = ("bin/", "deploy/", "doc/", "etc/", "standards/", "tools/")


def load_dependencies(path):
    """The dict assigned to `dependencies`, by ast.literal_eval (the file is never executed)."""
    with open(path, encoding="utf-8") as fd:
        tree = ast.parse(fd.read(), filename=path)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "dependencies"
                                                for t in node.targets):
            return ast.literal_eval(node.value)
    raise UsageError("no `dependencies` dict in setup_dependencies.py")


def closure(deps, name, seen=None):
    """packages, modules, statics, bins shipped by a system, following its 'systems'."""
    seen = seen if seen is not None else set()
    empty = (set(), set(), set(), set())
    if name in seen or name not in deps:
        return empty
    seen.add(name)
    entry = deps[name]
    pkgs, mods = set(entry.get("packages", [])), set(entry.get("modules", []))
    stats, bins = set(entry.get("statics", [])), set(entry.get("bin", []))
    for sub in entry.get("systems", []):
        p2, m2, s2, b2 = closure(deps, sub, seen)
        pkgs |= p2
        mods |= m2
        stats |= s2
        bins |= b2
    return pkgs, mods, stats, bins


def module_of(path):
    if not path.startswith("src/python/") or not path.endswith(".py"):
        return None
    module = path[len("src/python/"):-3].replace("/", ".")
    if module.endswith(".__init__"):
        module = module[:-len(".__init__")]
    return module


def ships_module(pkgs, mods, module):
    if module in mods or module + ".__init__" in mods:
        return True
    parts = module.split(".")
    for pkg in pkgs:
        base = pkg.rstrip("+").split(".")
        if parts[:len(base)] != base:
            continue
        rest = len(parts) - len(base)
        # 'X+' = X and everything below; 'X' = X/__init__ and the modules directly in X
        if pkg.endswith("+") or rest <= 1:
            return True
    return False


def ships_static(stats, bins, path):
    for static in stats:
        if static.endswith("+"):
            base = static[:-1]
            if path == base or path.startswith(base + "/"):
                return True
        elif posixpath.dirname(path) == static:
            return True
    return any(path == "bin/" + name for name in bins)


def file_group(path):
    """patchComponent.sh file group (bin/patchComponent.sh L371-374)."""
    if path.startswith("src/python/"):
        return "src"
    if path.startswith("test/"):
        return "test"
    if path.startswith(TOPLEVEL_DIRS):
        return "toplevel"
    if path.startswith("src/"):
        return "static"
    return None


def classify(entry, deps, closures=None):
    """One changed file -> {path, scope, group, patchable, why} (pure)."""
    path = str(entry.get("filename", ""))
    closures = closures or service_closures(deps)
    group = file_group(path)
    binary = bool(entry.get("binary"))
    if group is None:
        patchable, why = False, "no patchComponent.sh destination"
    elif binary:
        patchable, why = False, "binary file"
    else:
        patchable, why = True, ""
    agent = central = False
    if not path.startswith("test/"):
        module = module_of(path)
        for svc, (pkgs, mods, stats, bins) in closures.items():
            hit = ships_module(pkgs, mods, module) if module else ships_static(stats, bins, path)
            if hit and SERVICES[svc] == "AGENT":
                agent = True
            elif hit:
                central = True
    scope = "BOTH" if agent and central else "AGENT" if agent else "CENTRAL" if central else "NONE"
    return {"path": path, "scope": scope, "group": group, "patchable": patchable, "why": why}


def service_closures(deps):
    return {svc: closure(deps, svc) for svc in SERVICES}


def pr_scope(scopes):
    scopes = set(scopes) - {"NONE"}
    if "BOTH" in scopes or {"AGENT", "CENTRAL"} <= scopes:
        return "BOTH"
    if scopes:
        return scopes.pop()
    return "NONE"


def files_decision(entries, deps):
    """Classify a PR file list and decide (pure). Returns the stdout dict without source."""
    closures = service_closures(deps)
    files = [classify(e, deps, closures) for e in entries]
    scope = pr_scope(f["scope"] for f in files)
    reasons, bad = [], []
    for f in files:
        if not f["patchable"]:
            reasons.append("%s: %s" % (f["path"], f["why"]))
            bad.append({"path": f["path"], "why": f["why"]})
    patchable = not bad
    if len(entries) > MAX_FILES:
        reasons.insert(0, "too many files (%d, at most %d)" % (len(entries), MAX_FILES))
    if scope not in ("AGENT", "BOTH"):
        reasons.append("the PR changes no agent code (scope %s)" % scope)
    go = patchable and scope in ("AGENT", "BOTH") and len(entries) <= MAX_FILES
    return {"go": go, "scope": scope, "patchable": patchable, "reasons": reasons,
            "bad_files": bad, "files": files}


def cmd_files(args):
    state = load_state(args.work)
    if state is None:
        raise UsageError("no state: run check first")
    if state.get("stop"):
        return skipped("files", state)
    try:
        real_entries = load_json(args.pr_files)
    except (OSError, ValueError):
        raise UsageError("--pr-files is not a readable JSON list") from None
    if not isinstance(real_entries, list):
        raise UsageError("--pr-files is not a JSON list")
    begin_stage(args.work, state, "files")
    deps = load_dependencies(args.setup_deps or os.path.join(default_repo_root(), "setup_dependencies.py"))
    run = state["run"]
    real = files_decision(real_entries, deps)
    if run["mode"] == "fake":
        table = load_json(os.path.join(run["fixtures"], "pr_files.json"))
        result = files_decision(table.get(run["scenario"], table["default"]), deps)
        result["source"] = "fixture"
        result["real_files"] = real["files"]
    else:
        result = real
        result["source"] = "pull request"
    if not result["go"]:
        set_stop(state, "files", "not-patchable", result["reasons"])
    end_stage(args.work, state, "files", result)
    emit(result)
    return 0


# ---------------------------------------------------------------------------
# inject
# ---------------------------------------------------------------------------
def build_inject_argv(mode, req_string, repo_root):
    """The exact inject-test-wfs.py command (pure)."""
    argv = [sys.executable, os.path.join(repo_root, INJECT_SCRIPT),
            "-u", TESTBED_URL, "-m", TEMPLATE_MODE, "-f", TEMPLATE, "-c", CAMPAIGN,
            "-r", req_string, "-t", TEAM, "-s", SITE, "-a", ACQ_ERA, "-p", PROC_STR, "-v", PROC_VER]
    if mode not in ("inject-only", "assign"):
        argv.append("--dryRun")
    if mode == "inject-only":
        argv.append("--injectOnly")
    return argv


def child_env(parent=None):
    """Only a few safe variables; the X509 ones point nowhere; never a token."""
    parent = os.environ if parent is None else parent
    env = {k: parent[k] for k in ("PATH", "HOME", "LANG", "LC_ALL") if k in parent}
    env.update({"X509_USER_CERT": "/nonexistent", "X509_USER_KEY": "/nonexistent",
                "X509_USER_PROXY": "/nonexistent"})
    return env


def run_child(argv, cwd, env, log_path, timeout):
    """Run the child, raw output to log_path only. Returns the exit code."""
    with open(log_path, "w", encoding="utf-8") as fd:
        proc = subprocess.run(argv, cwd=cwd, env=env, stdout=fd, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL, timeout=timeout, check=False)
    return proc.returncode


def tweaked_json_path():
    """The temp file inject-test-wfs.py writes (L221). Never printed."""
    return "/tmp/%s.json" % pwd.getpwuid(os.getuid()).pw_name


def invent_request_name(requestor, request_string, now):
    """Fake name in the shape of Request.py generateRequestName (UTC, 4-digit suffix)."""
    stamp = time.strftime("%y%m%d_%H%M%S", time.gmtime(now))
    name = "%s_%s_%s_%04d" % (requestor, request_string, stamp, random.randint(0, 9999))
    return name[:150]


def parse_request_name(log_text):
    matches = NAME_RE.findall(log_text or "")
    return matches[0] if len(matches) == 1 else None


def build_sandbox(work, fixtures):
    sandbox = os.path.join(work, "sandbox")
    shutil.rmtree(sandbox, ignore_errors=True)
    folder = os.path.join(sandbox, "WMCore", "test", "data", "ReqMgr", "requests", TEMPLATE_MODE)
    os.makedirs(folder)
    shutil.copyfile(os.path.join(fixtures, TEMPLATE), os.path.join(folder, TEMPLATE))
    return sandbox


def sandbox_ok(sandbox):
    wmcore = os.path.join(sandbox, "WMCore")
    template = os.path.join(wmcore, "test", "data", "ReqMgr", "requests", TEMPLATE_MODE, TEMPLATE)
    return (os.path.isdir(wmcore) and not os.path.islink(wmcore)
            and os.path.isfile(template) and not os.path.islink(template))


def dry_run_problems(rc, log_text, tweaked, req_string, sandbox):
    """What is wrong with a dry run (pure apart from the .git check)."""
    problems = []
    if ("WMCore directory found" not in log_text or "successfully cloned" in log_text
            or "Failed to clone" in log_text or os.path.exists(os.path.join(sandbox, "WMCore", ".git"))):
        return ["the script tried to clone WMCore"]
    if rc != 0:
        problems.append("inject-test-wfs.py exited %s" % rc)
    if "dry-run command:" not in log_text:
        problems.append("no 'dry-run command:' line in the inject log")
    if tweaked is None:
        problems.append("the tweaked template was not written")
        return problems
    create, assign = tweaked.get("createRequest", {}), tweaked.get("assignRequest", {})
    want = [(create.get("Campaign"), CAMPAIGN, "Campaign"),
            (create.get("RequestString"), TEMPLATE.split(".json")[0] + "_" + req_string, "RequestString"),
            (assign.get("Team"), TEAM, "Team"),
            (assign.get("SiteWhitelist"), [SITE], "SiteWhitelist")]
    for got, expected, field in want:
        if got != expected:
            problems.append("tweaked template has the wrong %s" % field)
    return problems


def cmd_inject(args):
    state = load_state(args.work)
    if state is None:
        raise UsageError("no state: run check first")
    if state.get("stop"):
        return skipped("inject", state)
    begin_stage(args.work, state, "inject")
    state["inject"]["started"] = False
    run = state["run"]
    mode = run["mode"]
    if mode not in BUILT_MODES:
        refuse(args.work, state, "inject", "mode %s not built in M1a" % mode)
    repo_root = os.path.abspath(args.repo_root or default_repo_root())
    argv = build_inject_argv(mode, run["req_string"], repo_root)
    shown = [redact(a) for a in argv]
    reasons = []
    if not os.path.isfile(argv[1]):
        reasons.append("inject-test-wfs.py not found in the checkout")
    sandbox = build_sandbox(args.work, run["fixtures"])
    if not reasons and not sandbox_ok(sandbox):
        reasons.append("sandbox not built")
    if reasons:
        return _inject_stop(args.work, state, argv, shown, reasons)
    state["inject"]["started"] = True
    save_state(args.work, state)
    if BUILT_MODES == {"fake"} and "--dryRun" not in argv:
        # third guard; an explicit raise, so python -O cannot remove it
        raise AssertionError("M1a: refusing to start inject-test-wfs.py without --dryRun")
    log_path = os.path.join(args.work, "inject.log")
    tweaked_path = tweaked_json_path()
    started = time.time()
    try:
        rc = run_child(argv, sandbox, child_env(), log_path, CHILD_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        rc = "timeout"
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fd:
            log_text = fd.read()
    except OSError:
        log_text = ""
    tweaked = None
    try:
        if os.path.getmtime(tweaked_path) >= started - 2:
            tweaked = load_json(tweaked_path)
    except (OSError, ValueError):
        tweaked = None
    reasons = dry_run_problems(rc, log_text, tweaked, run["req_string"], sandbox)
    if reasons:
        return _inject_stop(args.work, state, argv, shown, reasons)
    name = invent_request_name("gha_fake", "SC_ProdPsi_small_" + run["req_string"], time.time())
    result = {"ok": True, "dry_run": "--dryRun" in argv, "inject_only": "--injectOnly" in argv,
              "request": name, "argv": shown, "reasons": [], "time": int(time.time())}
    end_stage(args.work, state, "inject", result)
    emit(result)
    return 0


def _inject_stop(work, state, argv, shown, reasons):
    set_stop(state, "inject", "error", reasons)
    result = {"ok": False, "dry_run": "--dryRun" in argv, "inject_only": "--injectOnly" in argv,
              "request": None, "argv": shown, "reasons": reasons}
    end_stage(work, state, "inject", result)
    emit(result)
    return 0


# ---------------------------------------------------------------------------
# wait: fake clock, fake source, job counts, verdict
# ---------------------------------------------------------------------------
class FakeClock(object):
    """Fake time: sleep(s) sleeps HOP_S real seconds and moves the fake time by s."""

    def __init__(self, start):
        self.time = float(start)

    def now(self):
        return self.time

    def sleep(self, seconds):
        if HOP_S > 0:
            time.sleep(HOP_S)
        self.time += seconds


def _rename(obj, old, new):
    if isinstance(obj, dict):
        return {_rename(k, old, new): _rename(v, old, new) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_rename(v, old, new) for v in obj]
    if isinstance(obj, str):
        return obj.replace(old, new)
    return obj


class FakeSource(object):
    """Answers from the fixture files (fake mode only). The M2 real source has the same methods."""

    def __init__(self, fixtures_dir, scenario, request_name, clock, dbs_creation_date=None):
        self.clock = clock
        self.scenario = scenario
        self.name = request_name or FIXTURE_NAME
        self.dbs_creation_date = dbs_creation_date
        self.timeline = FAKE_TIMELINES.get(scenario, FAKE_TIMELINES["pass"])
        reqmgr = load_json(os.path.join(fixtures_dir, "reqmgr2_request.json"))
        self._fixture_doc = reqmgr["result"][0][FIXTURE_NAME]
        self.doc = _rename(copy.deepcopy(self._fixture_doc), FIXTURE_NAME, self.name)
        wmstats = load_json(os.path.join(fixtures_dir, "wmstats_request.json"))
        self.wmstats_doc = _rename(wmstats["result"][0][FIXTURE_NAME], FIXTURE_NAME, self.name)
        self.agent_info = load_json(os.path.join(fixtures_dir, "wmstats_agent_info.json"))
        self.dbs = load_json(os.path.join(fixtures_dir, "dbs_files.json"))
        self.dn = self._fixture_doc["RequestTransition"][0].get("DN", "")
        now = int(clock.now())
        self.doc["RequestTransition"] = [{"Status": s, "UpdateTime": now, "DN": self.dn}
                                         for s in ("new", "assignment-approved", "assigned")]
        self.doc["RequestStatus"] = "assigned"

    def _hop(self, poll):
        return self.timeline[min(poll, len(self.timeline) - 1)]

    def agent_rows(self):
        rows = copy.deepcopy(self.agent_info.get("rows", []))
        for row in rows:
            row["value"]["timestamp"] = int(self.clock.now())
        return rows

    def campaign_answer(self):
        if self.scenario != "busy":
            return {"result": []}
        doc = _rename(copy.deepcopy(self._fixture_doc), FIXTURE_NAME, OTHER_FAKE_NAME)
        doc["RequestStatus"] = "running-open"
        return {"result": [{OTHER_FAKE_NAME: doc}]}

    def request_doc(self, poll):
        status = self._hop(poll)[0]
        if status != self.doc["RequestStatus"]:
            self.doc["RequestStatus"] = status
            self.doc["RequestTransition"].append({"Status": status, "UpdateTime": int(self.clock.now()),
                                                  "DN": self.dn})
        return copy.deepcopy(self.doc)

    def wmstats_answer(self, poll):
        status, jobs, _ = self._hop(poll)
        doc = copy.deepcopy(self.wmstats_doc)
        doc["RequestStatus"] = self.doc["RequestStatus"]
        doc["RequestTransition"] = copy.deepcopy(self.doc["RequestTransition"])
        if status in WMSTATS_NO_JOB_INFO:
            doc.pop("AgentJobInfo", None)
        else:
            for info in doc.get("AgentJobInfo", {}).values():
                info["status"] = copy.deepcopy(jobs)
                info["timestamp"] = int(self.clock.now())
        return {"result": [{self.name: doc}]}

    def dbs_files(self, dataset, poll):
        count = self._hop(poll)[2]
        assert count <= len(self.dbs), "fake timeline asks for more DBS files than the fixture has"
        out = []
        for record in self.dbs[:count]:
            if record.get("dataset") != dataset:
                continue
            record = copy.deepcopy(record)
            record["creation_date"] = (self.dbs_creation_date if self.dbs_creation_date is not None
                                       else int(self.clock.now()))
            out.append(record)
        return out


def _num(value):
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        return sum(v for v in value.values() if isinstance(v, int) and not isinstance(v, bool))
    return 0


def job_counts(wmstats_answer):
    """Sum AgentJobInfo[*].status over agents (pure)."""
    total = dict.fromkeys(("success", "failure", "cooloff", "pending", "running", "queued", "paused"), 0)
    for item in (wmstats_answer or {}).get("result", []) or []:
        for doc in item.values():
            for info in ((doc or {}).get("AgentJobInfo") or {}).values():
                status = (info or {}).get("status") or {}
                submitted = status.get("submitted") if isinstance(status.get("submitted"), dict) else {}
                total["success"] += _num(status.get("success"))
                total["failure"] += _num(status.get("failure"))
                total["cooloff"] += _num(status.get("cooloff"))
                total["pending"] += _num(submitted.get("pending"))
                total["running"] += _num(submitted.get("running"))
                total["queued"] += _num(status.get("queued"))
                total["paused"] += _num(status.get("paused"))
    return total


def _fmt_h(hours):
    return ("%.2f" % hours).rstrip("0").rstrip(".")


def _missing(jobs, dbs_files):
    parts = []
    if jobs.get("success", 0) < MIN_SUCCESS:
        parts.append("only %d successful jobs" % jobs.get("success", 0))
    if dbs_files < 1:
        parts.append("no new file in DBS int")
    return parts


def decide(status, jobs, dbs_files, polls_since_completed, deadline_reached, deadline_h=None):
    """Verdict rule (spec 2.6). Returns (verdict, reasons)."""
    deadline_h = DEADLINE_H if deadline_h is None else deadline_h
    failure = jobs.get("failure", 0)
    if failure > 0:
        return "fail", ["%d failed job%s" % (failure, "" if failure == 1 else "s")]
    if status in FAILED_STATUSES:
        return "fail", ["request is %s" % status]
    done = status in COMPLETED_OR_LATER
    if done and jobs.get("success", 0) >= MIN_SUCCESS and dbs_files >= 1:
        return "pass", ["%s with %d successful jobs, 0 failed, %s in DBS int"
                        % (status, jobs["success"], plural(dbs_files, "new file"))]
    if deadline_reached:
        if done:
            return "fail", _missing(jobs, dbs_files)
        return "timeout", ["still %s after %s h" % (status, _fmt_h(deadline_h))]
    if done:
        if polls_since_completed < GRACE_POLLS:
            return "running", []
        return "fail", _missing(jobs, dbs_files)
    return "running", []


def run_wait(source, clock, name, deadline_s, poll_s, t0=None):
    """Poll until the verdict is not running (pure apart from the source and clock)."""
    t0 = clock.now() if t0 is None else t0
    polls = completed_polls = 0
    while True:
        doc = source.request_doc(polls)
        status = doc.get("RequestStatus")
        jobs = job_counts(source.wmstats_answer(polls))
        dbs_files = dbs_events = 0
        if status in COMPLETED_OR_LATER:
            completed_polls += 1
            transitions = doc.get("RequestTransition") or [{}]
            created = transitions[0].get("UpdateTime", 0)
            for dataset in doc.get("OutputDatasets") or []:
                for record in source.dbs_files(dataset, polls):
                    if record.get("creation_date", 0) >= created:
                        dbs_files += 1
                        dbs_events += int(record.get("event_count") or 0)
        polls += 1
        elapsed = clock.now() - t0
        verdict, reasons = decide(status, jobs, dbs_files, max(completed_polls - 1, 0),
                                  elapsed >= deadline_s, deadline_s / 3600.0)
        if verdict != "running":
            break
        clock.sleep(min(poll_s, deadline_s - elapsed))
    transitions = doc.get("RequestTransition") or []
    first = transitions[0].get("UpdateTime", t0) if transitions else t0
    timeline = [[t.get("Status"), int(round((t.get("UpdateTime", first) - first) / 60.0))]
                for t in transitions]
    return {"verdict": verdict, "final_status": status, "jobs": jobs, "dbs_files": dbs_files,
            "dbs_events": dbs_events, "polls": polls, "elapsed_s": int(elapsed),
            "reasons": reasons, "timeline": timeline, "request": name}


def cmd_wait(args):
    state = load_state(args.work)
    if state is None:
        raise UsageError("no state: run check first")
    if state.get("stop"):
        return skipped("wait", state)
    begin_stage(args.work, state, "wait")
    run = state["run"]
    if run["mode"] not in BUILT_MODES:
        refuse(args.work, state, "wait", "mode %s not built in M1a" % run["mode"])
    name = (state.get("inject") or {}).get("request")
    if not name:
        raise UsageError("no request name: run inject first")
    clock = FakeClock(time.time())
    source = FakeSource(run["fixtures"], run["scenario"], name, clock)
    result = run_wait(source, clock, name, DEADLINE_H * 3600.0, POLL_MIN * 60.0)
    result["fake_clock"] = True
    end_stage(args.work, state, "wait", result)
    emit(result)
    return 0


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------
def cleanup_action(status):
    """Return (action, note) from the request status (RequestStatus.py transitions)."""
    if status in REJECT_FROM:
        return "reject", ""
    if status in ABORT_FROM:
        return "abort", ""
    if status == "force-complete":
        return "none", "reject by hand after completed"
    if status:
        return "none", "request is already %s" % status
    return "none", "unknown status"


def cleanup_result(state):
    """What cleanup does for this state (pure). M1a never sends."""
    if state is None:
        return {"action": "none", "note": "no state"}
    inject = state.get("inject") or {}
    name = inject.get("request")
    if not name:
        if inject.get("started"):
            return {"action": "unknown", "sent": False,
                    "note": "a request may exist: look for campaign %s by hand" % CAMPAIGN}
        return {"action": "none", "note": "nothing was injected"}
    wait = state.get("wait") or {}
    status = wait.get("final_status")
    assumed = status is None
    if assumed:
        status = "assigned"   # what inject leaves behind in the fake (and assign) path
    action, note = cleanup_action(status)
    mode = state.get("run", {}).get("mode")
    result = {"action": action, "from_status": status, "request": name, "sent": False}
    if action in ("reject", "abort"):
        body = json.dumps({"RequestStatus": "rejected" if action == "reject" else "aborted"})
        result["would_send"] = "PUT /reqmgr2/data/request/%s %s" % (name, body)
        if mode in ("fake", "readonly"):
            note = "%s mode: not sent" % mode
        else:
            note = "mode %s not built in M1a: not sent" % mode
    if assumed:
        note = (note + "; " if note else "") + "status not read, assumed assigned"
    result["note"] = note
    return result


def cmd_cleanup(args):
    state = load_state(args.work)
    if state is None:
        emit(cleanup_result(None))
        return 0
    begin_stage(args.work, state, "cleanup")
    result = cleanup_result(state)
    end_stage(args.work, state, "cleanup", result)
    emit(result)
    return 0


# ---------------------------------------------------------------------------
# report: verdict, status, comment
# ---------------------------------------------------------------------------
TITLES = {"pass": "PASS", "fail": "FAIL", "timeout": "TIMEOUT", "busy": "NOT STARTED: testbed busy",
          "not-patchable": "NOT STARTED: not patchable", "dry-run": "DRY RUN", "error": "ERROR"}
STATES = {"pass": "success", "fail": "failure", "timeout": "error", "busy": "error",
          "not-patchable": "error", "dry-run": "success", "error": "error"}


def final_verdict(state):
    """Return {verdict, stage, reasons, kind} for a state (pure)."""
    if state is None:
        return {"verdict": "error", "stage": "check", "reasons": ["no state: pre-flight did not start"]}
    stop = state.get("stop")
    if stop:
        return {"verdict": stop.get("verdict", "error"), "stage": stop.get("stage", "?"),
                "reasons": list(stop.get("reasons") or []), "kind": stop.get("kind")}
    for stage in STAGES:
        info = state.get(stage)
        if info is None:
            return {"verdict": "error", "stage": stage, "reasons": ["stage %s did not run" % stage]}
        if not info.get("done"):
            why = "stage %s did not finish" % stage
            if info.get("error"):
                why += ": %s" % info["error"]
            return {"verdict": "error", "stage": stage, "reasons": [why]}
    wait = state["wait"]
    verdict = wait.get("verdict")
    if verdict not in TITLES:
        return {"verdict": "error", "stage": "wait", "reasons": ["wait ended with verdict %s" % verdict]}
    return {"verdict": verdict, "stage": "wait", "reasons": list(wait.get("reasons") or [])}


def plain(text):
    """Clean a plain-text value: one line, no backticks or pipes, no HTML."""
    text = str(text).replace("\r", " ").replace("\n", " ").replace("`", "")
    return text.replace("|", "/").replace("<", "(").replace(">", ")")


def md_code(text):
    """PR-supplied text as one inert code span."""
    text = "" if text is None else str(text)
    text = text.replace("\r", "\\n").replace("\n", "\\n").replace("`", "")
    if not text:
        return "-"
    if len(text) > 200:
        text = text[:200] + "..."
    return "`" + text.replace("|", "\\|") + "`"


def status_for(state, fv):
    """status.json content (pure)."""
    verdict = fv["verdict"]
    first = plain(fv["reasons"][0]) if fv["reasons"] else ""
    if verdict == "pass":
        wait = state["wait"]
        desc = "PASS: %s, %d jobs ok, %d new files in DBS int" % (
            wait.get("final_status"), wait["jobs"].get("success", 0), wait.get("dbs_files", 0))
    elif verdict == "fail":
        desc = "FAIL: %s" % first
    elif verdict == "timeout":
        desc = "TIMEOUT: %s" % first
    elif verdict == "busy":
        n = len((state.get("check") or {}).get("active_requests") or [])
        desc = "NOT STARTED: testbed busy (%d active %s request%s)" % (n, CAMPAIGN, "" if n == 1 else "s")
    elif verdict == "not-patchable":
        desc = "NOT STARTED: PR cannot be put on the agent (%s)" % first
    elif verdict == "dry-run":
        desc = "DRY RUN: pre-flight ok, nothing injected"
    else:
        desc = "ERROR in %s: %s" % (fv.get("stage", "?"), first)
    mode = ((state or {}).get("run") or {}).get("mode")
    if mode == "fake":
        desc = "FAKE: " + desc
    desc = redact(desc)
    if len(desc) > 140:
        desc = desc[:137] + "..."
    return {"state": STATES.get(verdict, "error"), "description": desc, "verdict": verdict,
            "context": STATUS_CONTEXT}


def _v(value):
    return "-" if value in (None, "", []) else value


def _waited(wait):
    if not wait or "elapsed_s" not in wait:
        return "-"
    minutes = int(wait["elapsed_s"] // 60)
    hours, minutes = divmod(minutes, 60)
    text = ("%d h %d min" % (hours, minutes)) if hours else ("%d min" % minutes)
    text += ", %s" % plural(wait.get("polls", 0), "poll")
    if wait.get("fake_clock"):
        text += " (fake clock)"
    return text


def _file_rows(files):
    rows = ["| file | runs on | patchable | why |", "|---|---|---|---|"]
    for f in files[:MAX_FILE_ROWS]:
        rows.append("| %s | %s | %s | %s |" % (md_code(f.get("path")), f.get("scope", "-"),
                                              "yes" if f.get("patchable") else "no", _v(f.get("why"))))
    if len(files) > MAX_FILE_ROWS:
        rows.append("")
        rows.append("and %d more" % (len(files) - MAX_FILE_ROWS))
    return rows


def _why_lines(state, fv):
    if fv.get("stage") == "files" and fv["verdict"] == "not-patchable":
        files = state.get("files") or {}
        lines = ["- %s: %s" % (md_code(b["path"]), plain(b["why"])) for b in files.get("bad_files", [])]
        lines += ["- %s" % plain(r) for r in files.get("reasons", []) if not any(
            r.startswith(b["path"] + ": ") for b in files.get("bad_files", []))]
        return lines[:60]
    return ["- %s" % plain(r) for r in fv["reasons"][:30]] or ["- -"]


def render_comment(state, log_text=None):
    """The one PR comment (pure). Built from structured fields only."""
    fv = final_verdict(state)
    state = state or {}
    run = state.get("run") or {}
    verdict = fv["verdict"]
    title = TITLES.get(verdict, "ERROR")
    if verdict == "error" and fv.get("kind") == "agent":
        title = "NOT STARTED: agent not ready"
    lines = [MARKER, "### Injection test: %s" % title]
    inject = state.get("inject") or {}
    cleanup = state.get("cleanup") or {}
    if inject.get("request") and run.get("mode") not in ("fake", "readonly") and not cleanup.get("sent"):
        lines.append("**WARNING:** request %s was not cleaned up; reject or abort it by hand."
                     % md_code(inject["request"]))
    if run.get("mode") == "fake":
        lines.append("> **FAKE MODE** - nothing was sent to the testbed. Every answer below comes from "
                     "fixture files (scenario `%s`)." % plain(run.get("scenario", "-")))
        lines.append(">")
    lines.append("> Plumbing only: the testbed agent runs its released WMCore. "
                 "The code of this PR was NOT deployed.")
    wait = state.get("wait") or {}
    jobs = wait.get("jobs") or {}
    run_link = "[%s](%s)" % (run["run"], run["run_url"]) if run.get("run") and run.get("run_url") else "-"
    jobs_text = ("success %d, failure %d, cooloff %d, pending %d, running %d"
                 % tuple(jobs.get(k, 0) for k in ("success", "failure", "cooloff", "pending", "running"))
                 if jobs else "-")
    dbs_text = ("%s, %d events" % (plural(wait.get("dbs_files", 0), "new file"), wait.get("dbs_events", 0))
                if "dbs_files" in wait else "-")
    if cleanup.get("action") in ("reject", "abort"):
        cleanup_text = "%s from `%s` (%s)" % (cleanup["action"], plain(cleanup.get("from_status")),
                                             plain(cleanup.get("note", "")))
    elif cleanup:
        cleanup_text = "%s (%s)" % (cleanup.get("action", "-"), plain(cleanup.get("note", "")))
    else:
        cleanup_text = "-"
    pr_text = "#%s at `%s`" % (run["pr"], run.get("sha7", "-")) if run.get("pr") else "-"
    lines += ["", "| | |", "|---|---|",
              "| PR / commit | %s |" % pr_text,
              "| Mode | %s |" % ("`%s`" % run["mode"] if run.get("mode") else "-"),
              "| Run | %s |" % run_link,
              "| Request | %s |" % md_code(inject.get("request")),
              "| Final status | %s |" % ("`%s`" % plain(wait["final_status"]) if wait.get("final_status") else "-"),
              "| Jobs | %s |" % jobs_text,
              "| DBS int | %s |" % dbs_text,
              "| Waited | %s |" % _waited(wait),
              "| Cleanup | %s |" % cleanup_text,
              "", "**Why**"]
    lines += _why_lines(state, fv)
    check = state.get("check") or {}
    lines += ["", "<details><summary>Pre-flight</summary>", ""]
    if check.get("agents"):
        lines += ["| agent | team | status | drain | down | disk | age |", "|---|---|---|---|---|---|---|"]
        for a in check["agents"]:
            lines.append("| %s | %s | %s | %s | %s | %s | %s s |" % (
                md_code(a.get("agent_url")), md_code(a.get("team")), plain(a.get("status")),
                "yes" if a.get("drain_mode") else "no", a.get("down", 0), a.get("disk", 0), a.get("age_s", 0)))
    else:
        lines.append("No agent information.")
    active = check.get("active_requests") or []
    lines += ["", "Active %s requests: %s" % (CAMPAIGN, ", ".join(md_code(n) for n in active) if active else "none"),
              "", "</details>"]
    files = state.get("files") or {}
    if files.get("files") is not None:
        lines += ["", "<details><summary>Changed files (%d)</summary>" % len(files["files"]), ""]
        if files.get("source") == "fixture":
            lines += ["Fake mode: the decision used this fixture list.", ""]
        lines += _file_rows(files["files"])
        if files.get("source") == "fixture":
            real = files.get("real_files") or []
            lines += ["", "The real files of this PR (%d), shown for information:" % len(real), ""]
            lines += _file_rows(real)
        lines += ["", "</details>"]
    if wait.get("timeline"):
        lines += ["", "<details><summary>Status timeline</summary>", "",
                  "| status | minutes after injection |", "|---|---|"]
        for status, minutes in wait["timeline"]:
            lines.append("| `%s` | %s |" % (plain(status), minutes))
        lines += ["", "</details>"]
    if verdict == "error" and log_text:
        tail = log_tail(log_text)
        if tail:
            lines += ["", "<details><summary>Inject log (last lines)</summary>", "", "```"] + tail + ["```", "",
                                                                                                    "</details>"]
    who = "Requested by @%s. " % plain(run["login"]) if run.get("login") else ""
    lines += ["", "_%sThe next run on this PR edits this comment._" % who]
    text = redact("\n".join(lines) + "\n")
    if len(text) > MAX_COMMENT:
        text = text[:MAX_COMMENT - 40] + "\n\n(comment cut: too long)\n"
    return text


def public_state(state):
    """The uploaded copy of the state: redacted, and no absolute paths of the runner."""
    state = copy.deepcopy(state) if state is not None else {}
    run = state.get("run") or {}
    if run.get("fixtures"):
        run["fixtures"] = os.path.basename(run["fixtures"])
    inject = state.get("inject") or {}
    if inject.get("argv"):
        inject["argv"] = [os.path.basename(a) if os.path.isabs(a) else a for a in inject["argv"]]
    return redact_obj(state)


def cmd_report(args):
    state = load_state(args.work)
    log_text = None
    try:
        with open(os.path.join(args.work, "inject.log"), encoding="utf-8", errors="replace") as fd:
            log_text = fd.read()
    except OSError:
        pass
    fv = final_verdict(state)
    status = status_for(state, fv)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "comment.md"), "w", encoding="utf-8") as fd:
        fd.write(render_comment(state, log_text))
    with open(os.path.join(args.out, "status.json"), "w", encoding="utf-8") as fd:
        json.dump(status, fd)
    with open(os.path.join(args.out, "state.json"), "w", encoding="utf-8") as fd:
        json.dump(public_state(state), fd, indent=1)
    emit(status)
    return 0


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------
def build_parser():
    parser = argparse.ArgumentParser(prog="inject_test.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd")
    sub.required = True
    gate = sub.add_parser("gate", help="parse the comment or dispatch and decide (pure)")
    gate.add_argument("--event", required=True)
    gate.add_argument("--login", required=True)
    gate.add_argument("--actor", required=True)
    gate.add_argument("--ref", required=True)
    gate.add_argument("--users", default="")
    gate.add_argument("--ceiling", default="")
    gate.add_argument("--requested", default="")
    gate.add_argument("--scenario", default="pass")
    gate.add_argument("--github-output", default=None)
    gate.set_defaults(func=cmd_gate)
    check = sub.add_parser("check", help="pre-flight")
    for name in ("--work", "--mode", "--pr", "--sha", "--run", "--run-url", "--login", "--actor"):
        check.add_argument(name, required=True)
    check.add_argument("--users", default="")
    check.add_argument("--scenario", default="pass")
    check.add_argument("--fixtures", default=None)
    check.set_defaults(func=cmd_check)
    files = sub.add_parser("files", help="classify the changed files")
    files.add_argument("--work", required=True)
    files.add_argument("--pr-files", required=True)
    files.add_argument("--setup-deps", default=None)
    files.set_defaults(func=cmd_files)
    inject = sub.add_parser("inject", help="run inject-test-wfs.py (M1a: --dryRun only)")
    inject.add_argument("--work", required=True)
    inject.add_argument("--repo-root", default=None)
    inject.set_defaults(func=cmd_inject)
    for name, func in (("wait", cmd_wait), ("cleanup", cmd_cleanup)):
        cmd = sub.add_parser(name)
        cmd.add_argument("--work", required=True)
        cmd.set_defaults(func=func)
    report = sub.add_parser("report", help="write comment.md, status.json, state.json")
    report.add_argument("--work", required=True)
    report.add_argument("--out", required=True)
    report.set_defaults(func=cmd_report)
    return parser


def _note_crash(work, message):
    """Record a crash message on the running stage (done stays false)."""
    try:
        state = load_state(work)
        stage = state and state.get("stage")
        if stage and isinstance(state.get(stage), dict) and not state[stage].get("done"):
            state[stage]["error"] = redact(message)[:200]
            save_state(work, state)
    except Exception:  # pylint: disable=broad-except
        pass


def main(argv=None):
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        configure(os.environ)
        return args.func(args)
    except UsageError as exc:
        log("usage: %s" % exc)
        return 2
    except Refused as exc:
        log("refused: %s" % exc)
        return 3
    except Exception as exc:  # pylint: disable=broad-except
        message = "%s: %s" % (type(exc).__name__, exc)
        log("internal error: %s" % message)
        if getattr(args, "work", None):
            _note_crash(args.work, message)
        return 1


if __name__ == "__main__":
    sys.exit(main())
