#!/usr/bin/env python3
"""
Unit tests for inject_test.py (stage M1a). No network.

    python3 -m pytest .github/ci/inject
    python3 -m unittest discover -s .github/ci/inject -p "test_*.py"
"""

import ast
import contextlib
import copy
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, "..", "..", ".."))
FIXTURES = os.path.join(HERE, "fixtures")
MODULE_PATH = os.path.join(HERE, "inject_test.py")
AGENT_SH = os.path.join(HERE, "agent.sh")
SHA = "abc1234" + "0" * 33
DN = "/DC=ch/DC=cern/OU=Organic Units/OU=Users/CN=ghafake/CN=000000/CN=GHA Fake User"


def _load_module():
    spec = importlib.util.spec_from_file_location("inject_test_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


IT = _load_module()
IT.HOP_S = 0


def _no_net(*args, **kwargs):
    raise AssertionError("network call attempted in a unit test")


@contextlib.contextmanager
def no_network():
    """Every in-process connection attempt fails the test."""
    with mock.patch.object(socket.socket, "connect", _no_net), \
            mock.patch.object(socket, "create_connection", _no_net), \
            mock.patch.object(urllib.request, "urlopen", _no_net):
        yield


def run_main(argv, env=None):
    """Call main() in-process. Returns (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    environ = {"WMCI_INJECT_HOP_S": "0"}
    environ.update(env or {})
    with mock.patch.dict(os.environ, environ), contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        code = IT.main(argv)
    return code, out.getvalue(), err.getvalue()


def last_json(text):
    return json.loads(text.strip().splitlines()[-1])


def check_argv(work, scenario="pass", mode="fake", actor="alice", users="alice", sha=SHA, pr="12", run="123",
               ceiling="fake"):
    return ["check", "--work", work, "--mode", mode, "--pr", pr, "--sha", sha, "--run", run,
            "--run-url", "https://github.com/o/r/actions/runs/123", "--login", "alice",
            "--actor", actor, "--users", users, "--ceiling", ceiling, "--scenario", scenario]


def old_trigger_regex():
    """The own-line pattern of wmcore-pr-comment-trigger.yml (grep -qiE, one line at a time), as Python."""
    path = os.path.join(REPO, ".github", "workflows", "wmcore-pr-comment-trigger.yml")
    with open(path) as fd:
        found = re.findall(r"grep -qiE '([^']*gha test please[^']*)'", fd.read())
    assert len(found) == 1, found
    return re.compile(found[0].replace("[:space:]", r" \t\r\n\f\v"), re.IGNORECASE)


GOOD_LOG = [
    "2026-10-06 12:00:00,000:INFO:inject-test-wfs: WMCore directory found. I'm not going to clone it again.",
    "2026-10-06 12:00:05,000:INFO:inject-test-wfs: Processing template: SC_ProdPsi_small.json",
    "2026-10-06 12:00:05,001:INFO:inject-test-wfs: dry-run command: python3 reqmgr2.py -u "
    "https://cmsweb-testbed.cern.ch -f /tmp/someone.json -i -g ",
]


def make_child(tweaked_path, log_lines=None, make_git=False, calls=None, tweak=None):
    """A stub for run_child: writes a log and the tweaked template like inject-test-wfs.py --dryRun."""
    def run_child(argv, cwd, env, log_path, timeout):
        if calls is not None:
            calls.append({"argv": list(argv), "cwd": cwd, "env": dict(env)})
        path = os.path.join(cwd, "WMCore", "test", "data", "ReqMgr", "requests", "GHA", "SC_ProdPsi_small.json")
        with open(path) as fd:
            data = json.load(fd)
        data["createRequest"]["RequestString"] = "SC_ProdPsi_small_" + argv[argv.index("-r") + 1]
        data["createRequest"]["Campaign"] = argv[argv.index("-c") + 1]
        data["assignRequest"]["Team"] = argv[argv.index("-t") + 1]
        data["assignRequest"]["SiteWhitelist"] = argv[argv.index("-s") + 1].split(",")
        if tweak:
            tweak(data)
        with open(tweaked_path, "w") as fd:
            json.dump(data, fd)
        with open(log_path, "w") as fd:
            fd.write("\n".join(GOOD_LOG if log_lines is None else log_lines) + "\n")
        if make_git:
            os.makedirs(os.path.join(cwd, "WMCore", ".git"))
        return 0
    return run_child


class Base(unittest.TestCase):
    """Temp work folder; child runner stubbed; tweaked file in the temp folder."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wmci-inject-test-")
        self.work = os.path.join(self.tmp, "work")
        self.tweaked = os.path.join(self.tmp, "tweaked.json")
        self.calls = []
        self.patches = [mock.patch.object(IT, "tweaked_json_path", lambda: self.tweaked),
                        mock.patch.object(IT, "run_child", make_child(self.tweaked, calls=self.calls))]
        for p in self.patches:
            p.start()
        self.pr_files = os.path.join(self.tmp, "pr-files.json")
        with open(self.pr_files, "w") as fd:
            json.dump([{"filename": "src/python/WMCore/WorkQueue/WorkQueue.py", "status": "modified",
                        "changes": 3, "binary": False}], fd)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def stub_child(self, **kwargs):
        self.patches[1].stop()
        self.patches[1] = mock.patch.object(IT, "run_child", make_child(self.tweaked, calls=self.calls, **kwargs))
        self.patches[1].start()

    def state(self):
        with open(os.path.join(self.work, "state.json")) as fd:
            return json.load(fd)

    def write_state(self, state):
        with open(os.path.join(self.work, "state.json"), "w") as fd:
            json.dump(state, fd)

    def pipeline(self, scenario="pass", until="report"):
        """Run the sub-commands in order; returns {stage: (code, json)}."""
        out = {}
        steps = [("check", check_argv(self.work, scenario)),
                 ("files", ["files", "--work", self.work, "--pr-files", self.pr_files]),
                 ("inject", ["inject", "--work", self.work, "--repo-root", REPO]),
                 ("wait", ["wait", "--work", self.work]),
                 ("cleanup", ["cleanup", "--work", self.work]),
                 ("report", ["report", "--work", self.work, "--out", os.path.join(self.tmp, "out")])]
        with no_network():
            for name, argv in steps:
                code, stdout, _ = run_main(argv)
                out[name] = (code, last_json(stdout) if stdout.strip() else None)
                if name == until:
                    break
        return out

    def outputs(self):
        outdir = os.path.join(self.tmp, "out")
        with open(os.path.join(outdir, "comment.md")) as fd:
            comment = fd.read()
        with open(os.path.join(outdir, "status.json")) as fd:
            status = json.load(fd)
        with open(os.path.join(outdir, "state.json")) as fd:
            state_text = fd.read()
        return comment, status, state_text


# ---------------------------------------------------------------------------
class ParseCommandTest(unittest.TestCase):

    def test_matches(self):
        for body in ("gha injection test please", "GHA Injection Test Please!", "  gha injection test please.  ",
                     "gha injection test please\r\n", "thanks\ngha injection test please\nbye",
                     "```\ncode\n```\ngha injection test please"):
            self.assertEqual(IT.parse_command(body), "test", repr(body))

    def test_no_match(self):
        for body in ("please run gha injection test please", "gha injection test please, thanks",
                     "> gha injection test please", "`gha injection test please`", "gha  injection test please",
                     "gha injection test please: not started - mode off.", "gha test please", "",
                     "gha injection test please:"):
            self.assertEqual(IT.parse_command(body), "none", repr(body))

    def test_old_trigger_regex_does_not_match_new_phrase(self):
        old = old_trigger_regex()   # read from the real workflow file (F6)
        self.assertIsNotNone(old.search("gha test please"))
        self.assertIsNotNone(old.search("  GHA Test Please!"))
        for cmd in ("test", "status", "abort"):
            self.assertIsNone(old.search("gha injection %s please" % cmd))
        self.assertEqual(IT.parse_command("gha test please"), "none")
        self.assertIsNone(IT.CMD_RE.search("gha test please"))

    def test_status_abort_priority(self):
        self.assertEqual(IT.parse_command("gha injection status please"), "status")
        self.assertEqual(IT.parse_command("gha injection abort please"), "abort")
        self.assertEqual(IT.parse_command("gha injection test please\ngha injection abort please"), "abort")
        self.assertEqual(IT.parse_command("gha injection status please\ngha injection test please"), "test")

    def test_hidden_regions(self):
        for body in ("```\ngha injection test please\n```", "~~~\ngha injection test please\n~~~",
                     "```python\ngha injection test please", "<!-- gha injection test please -->",
                     "<!--\ngha injection test please\n-->", "<!-- open\ngha injection test please"):
            self.assertEqual(IT.parse_command(body), "none", repr(body))
        self.assertEqual(IT.parse_command("~~~\nx\n~~~\ngha injection test please"), "test")

    def test_code_shown_by_github(self):
        # S5: forms GitHub shows as code never start a run
        for body in ("type:\n\n    gha injection test please", "\tgha injection test please",
                     "- ```\n  gha injection test please\n  ```", "1. ```\n   gha injection test please\n   ```",
                     "> ```\n> gha injection test please\n> ```",
                     "<pre>\ngha injection test please\n</pre>", "<code>\ngha injection test please\n</code>",
                     "<PRE class='x'>\ngha injection test please\n</PRE>", "<pre>\ngha injection test please"):
            self.assertEqual(IT.parse_command(body), "none", repr(body))
        for body in ("- ```\n  x\n  ```\ngha injection test please", "a <code>x</code>\ngha injection test please",
                     "<pre>x</pre>\ngha injection test please", "   gha injection test please"):
            self.assertEqual(IT.parse_command(body), "test", repr(body))

    def test_unclosed_comment_not_at_line_start(self):
        # F5: an unclosed <!-- inside inline code or a closed fence hides nothing
        for body in ("use `<!--` to hide text\ngha injection test please",
                     "```\n<!-- my note\n```\ngha injection test please",
                     "use <code> tags\ngha injection test please"):
            self.assertEqual(IT.parse_command(body), "test", repr(body))


class UserAllowedTest(unittest.TestCase):

    def test_rules(self):
        self.assertTrue(IT.user_allowed("alice", "alice,bob"))
        self.assertTrue(IT.user_allowed("Alice", "ALICE"))
        self.assertTrue(IT.user_allowed("bob", " alice , bob "))
        self.assertFalse(IT.user_allowed("eve", "alice,bob"))
        self.assertFalse(IT.user_allowed("alice", ""))
        self.assertFalse(IT.user_allowed("alice", ","))
        self.assertFalse(IT.user_allowed("", ","))


class GateTest(unittest.TestCase):

    def gate(self, **kw):
        args = dict(event="issue_comment", body="gha injection test please", login="alice", actor="alice",
                    ref="refs/heads/master", users="alice", ceiling="fake", requested="", scenario="pass")
        args.update(kw)
        return IT.gate_decide(**args)

    def test_missing_ceiling_is_off(self):
        res = self.gate(ceiling="")
        self.assertEqual(res["mode"], "off")
        self.assertFalse(res["go"])

    def test_fake_ceiling(self):
        res = self.gate(event="workflow_dispatch", requested="ceiling")
        self.assertEqual((res["mode"], res["go"]), ("fake", True))
        self.assertEqual(IT.resolve_mode("fake", "ceiling"), ("fake", None))

    def test_refusals(self):
        self.assertIn("above the ceiling", IT.resolve_mode("fake", "assign")[1])
        self.assertIn("unknown mode", IT.resolve_mode("fake", "turbo")[1])
        self.assertEqual(IT.resolve_mode("weird", ""), ("off", None))
        res = self.gate(event="workflow_dispatch", requested="assign")
        self.assertFalse(res["go"])

    def test_not_built(self):
        res = self.gate(event="workflow_dispatch", ceiling="assign", requested="readonly")
        self.assertFalse(res["go"])
        self.assertTrue(any("not built in M1a" in r for r in res["reasons"]))

    def test_status_abort_not_built(self):
        for cmd in ("status", "abort"):
            res = self.gate(body="gha injection %s please" % cmd)
            self.assertEqual(res["command"], cmd)
            self.assertFalse(res["go"])
            self.assertIn("gha injection %s please is not built in M1a (planned for M1b)" % cmd, res["reasons"])

    def test_dispatch_ignores_body(self):
        res = self.gate(event="workflow_dispatch", body="nothing here", scenario="fail")
        self.assertEqual(res["command"], "test")
        self.assertEqual(res["scenario"], "fail")
        self.assertTrue(res["go"])

    def test_rerun_actor_not_allowed(self):
        res = self.gate(actor="mallory")
        self.assertFalse(res["allowed"])
        self.assertFalse(res["go"])

    def test_ref_allowed(self):
        self.assertTrue(IT.ref_allowed("refs/heads/master", "fake"))
        self.assertTrue(IT.ref_allowed("refs/heads/ci/injection-m1a", "fake"))
        self.assertFalse(IT.ref_allowed("refs/heads/feature", "fake"))
        self.assertFalse(IT.ref_allowed("refs/heads/ci/injection-m1a", "readonly"))
        self.assertTrue(IT.ref_allowed("refs/heads/master", "readonly"))
        res = self.gate(event="workflow_dispatch", ceiling="readonly", requested="readonly")
        self.assertFalse(res["go"])  # master is fine, but readonly is not built
        res = self.gate(ref="refs/heads/feature")
        self.assertFalse(res["go"])
        self.assertIn("mode fake is not allowed from ref refs/heads/feature", res["reasons"])

    def test_unknown_scenario(self):
        res = self.gate(event="workflow_dispatch", scenario="bogus")
        self.assertFalse(res["go"])
        self.assertIn("unknown scenario bogus", res["reasons"])

    def test_github_output(self):
        tmp = tempfile.mkdtemp()
        try:
            out = os.path.join(tmp, "out")
            code, stdout, _ = run_main(["gate", "--event", "issue_comment", "--login", "alice", "--actor", "alice",
                                        "--ref", "refs/heads/x\nevil=1", "--users", "alice", "--ceiling", "fake",
                                        "--github-output", out],
                                       env={"WMCI_COMMENT_BODY": "gha injection test please"})
            self.assertEqual(code, 0)
            self.assertFalse(last_json(stdout)["go"])
            with open(out) as fd:
                lines = fd.read().splitlines()
            self.assertEqual([l.split("=", 1)[0] for l in lines],
                             ["command", "allowed", "mode", "scenario", "go", "login", "reasons"])
            self.assertIn("go=false", lines)
        finally:
            shutil.rmtree(tmp)


# ---------------------------------------------------------------------------
class CheckTest(Base):

    def rows(self, **value):
        with open(os.path.join(FIXTURES, "wmstats_agent_info.json")) as fd:
            rows = json.load(fd)["rows"]
        rows[0]["value"]["timestamp"] = 1000000
        rows[0]["value"].update(value)
        return rows

    def test_agent_rules(self):
        ok, reasons, agents = IT.agent_ok(self.rows(), "testbed-vocms0263", 1000000)
        self.assertTrue(ok, reasons)
        self.assertEqual(agents[0]["agent_url"], "vocms0263.cern.ch")
        self.assertFalse(IT.agent_ok(self.rows(drain_mode=True), "testbed-vocms0263", 1000000)[0])
        ok, reasons, _ = IT.agent_ok(self.rows(down_components=["JobAccountant"]), "testbed-vocms0263", 1000000)
        self.assertFalse(ok)
        self.assertIn("JobAccountant", " ".join(reasons))
        disk = [{"filesystem": "/dev/sdb", "mounted": "/data1", "percent": "95%"}]
        ok, reasons, _ = IT.agent_ok(self.rows(disk_warning=disk), "testbed-vocms0263", 1000000)
        self.assertFalse(ok)
        self.assertIn("/data1", " ".join(reasons))
        self.assertFalse(IT.agent_ok(self.rows(status="down"), "testbed-vocms0263", 1000000)[0])
        ok, reasons, _ = IT.agent_ok(self.rows(status="error"), "testbed-vocms0263", 1000000)
        self.assertFalse(ok)
        self.assertIn("agent vocms0263.cern.ch status is error", reasons)
        ok, reasons, _ = IT.agent_ok(self.rows(), "testbed-vocms0263", 1000000 + 7200)
        self.assertFalse(ok)
        self.assertIn("agent info is 120 min old", reasons)
        ok, reasons, _ = IT.agent_ok(self.rows(), "other-team", 1000000)
        self.assertEqual(reasons, ["no agent of team other-team in WMStats"])

    def test_proxy_warning_not_copied(self):
        secret = "proxy expires soon: /srv/runner/grid-secrets/proxy"
        result = IT.agent_ok(self.rows(proxy_warning=secret, status="warning",
                                       down_component_detail=[{"name": "x", "log": "/secret/log"}]),
                             "testbed-vocms0263", 1000000)
        self.assertNotIn("grid-secrets", json.dumps(result))
        self.assertNotIn("/secret/log", json.dumps(result))

    def test_busy(self):
        self.assertEqual(IT.busy_requests({"result": []}), [])
        self.assertEqual(IT.busy_requests({"result": [{"a": {"RequestStatus": "running-open"}}]}), ["a"])
        self.assertEqual(IT.busy_requests({"result": [{"a": {"RequestStatus": "rejected-archived"}}]}), [])
        self.assertEqual(IT.busy_requests({"result": [{"a": {"RequestStatus": "running-open"}}]}, own_name="a"), [])

    def test_check_refusals(self):
        code, _, _ = run_main(check_argv(self.work, mode="readonly"))
        self.assertEqual(code, 3)
        self.assertEqual(self.state()["stop"]["verdict"], "error")
        shutil.rmtree(self.work)
        code, _, _ = run_main(check_argv(self.work, sha="ABC"))
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(os.path.join(self.work, "state.json")))

    def test_ceiling(self):
        # S3: a re-run keeps the gate's mode; the switch as it is now must still stop it
        for ceiling, reason in (("off", "WMCI_INJECT_MODE is off now"), ("", "WMCI_INJECT_MODE is off now"),
                                ("bogus", "WMCI_INJECT_MODE is off now")):
            shutil.rmtree(self.work, ignore_errors=True)
            code, _, _ = run_main(check_argv(self.work, ceiling=ceiling))
            self.assertEqual(code, 3, ceiling)
            self.assertEqual(self.state()["stop"]["reasons"], [reason])
            self.assertNotIn("agents", self.state()["check"])
        with mock.patch.object(IT, "BUILT_MODES", {"fake", "assign"}):
            shutil.rmtree(self.work, ignore_errors=True)
            code, _, _ = run_main(check_argv(self.work, mode="assign", ceiling="readonly"))
            self.assertEqual(code, 3)
            self.assertEqual(self.state()["stop"]["reasons"],
                             ["mode assign is above the WMCI_INJECT_MODE ceiling readonly"])
        for ceiling in ("fake", "assign"):
            shutil.rmtree(self.work, ignore_errors=True)
            code, stdout, _ = run_main(check_argv(self.work, ceiling=ceiling))
            self.assertEqual(code, 0, ceiling)
            self.assertTrue(last_json(stdout)["go"])

    def test_actor_not_in_users(self):
        code, _, err = run_main(check_argv(self.work, actor="mallory"))
        self.assertEqual(code, 3)
        state = self.state()
        self.assertEqual(state["stop"]["verdict"], "error")
        self.assertIn("started by mallory, who is not in WMCI_INJECT_USERS", state["stop"]["reasons"])
        self.assertTrue(state["check"]["done"])
        self.assertNotIn("agents", state["check"])
        self.assertIn("refused", err)

    def test_check_fake_go(self):
        code, stdout, _ = run_main(check_argv(self.work))
        self.assertEqual(code, 0)
        res = last_json(stdout)
        self.assertTrue(res["go"])
        self.assertEqual(res["agents"][0]["age_s"], 0)
        self.assertEqual(self.state()["run"]["req_string"], "GHA_INJ_PR12_abc1234_R123")


# ---------------------------------------------------------------------------
class ClassifyTest(Base):

    @classmethod
    def setUpClass(cls):
        cls.deps = IT.load_dependencies(os.path.join(REPO, "setup_dependencies.py"))

    def c(self, path, binary=False):
        return IT.classify({"filename": path, "binary": binary}, self.deps)

    def test_scopes(self):
        f = self.c("src/python/WMComponent/JobAccountant/AccountantWorker.py")
        self.assertEqual((f["scope"], f["group"], f["patchable"]), ("AGENT", "src", True))
        self.assertEqual(self.c("src/python/WMCore/ReqMgr/Service/Request.py")["scope"], "CENTRAL")
        self.assertEqual(self.c("src/python/WMCore/WorkQueue/WorkQueue.py")["scope"], "BOTH")
        self.assertEqual(self.c("src/python/WMCore/WMSpec/StdSpecs/StepChain.py")["scope"], "BOTH")
        self.assertEqual(self.c("src/python/WMCore/MicroService/MSTransferor/MSTransferor.py")["scope"], "CENTRAL")
        f = self.c("test/python/WMCore_t/Lexicon_t.py")
        self.assertEqual((f["scope"], f["group"], f["patchable"]), ("NONE", "test", True))
        f = self.c("src/couchapps/WMStatsAgent/views/x/map.js")
        self.assertEqual((f["scope"], f["group"], f["patchable"]), ("AGENT", "static", True))
        self.assertEqual(self.c("src/couchapps/WMStats/views/x/map.js")["scope"], "BOTH")
        f = self.c("bin/wmagent-resource-control")
        self.assertEqual((f["group"], f["patchable"], f["scope"]), ("toplevel", True, "AGENT"))

    def test_not_patchable(self):
        for path in ("setup.py", "requirements.txt", ".github/workflows/x.yml"):
            f = self.c(path)
            self.assertFalse(f["patchable"], path)
            self.assertEqual(f["why"], "no patchComponent.sh destination")
        f = self.c("doc/images/a.png", binary=True)
        self.assertEqual((f["group"], f["patchable"], f["why"]), ("toplevel", False, "binary file"))

    def test_pr_scope(self):
        res = IT.files_decision([{"filename": "src/python/WMComponent/JobAccountant/AccountantWorker.py"},
                                 {"filename": "src/python/WMCore/MicroService/MSTransferor/MSTransferor.py"}],
                                self.deps)
        self.assertEqual(res["scope"], "BOTH")
        self.assertTrue(res["go"])
        res = IT.files_decision([{"filename": "test/python/WMCore_t/Lexicon_t.py"}], self.deps)
        self.assertEqual(res["scope"], "NONE")
        self.assertFalse(res["go"])

    def test_too_many_files(self):
        entries = [{"filename": "src/python/WMComponent/JobAccountant/AccountantWorker.py"}] * 301
        res = IT.files_decision(entries, self.deps)
        self.assertFalse(res["go"])
        self.assertIn("too many files", res["reasons"][0])

    def test_deps_read_with_ast(self):
        marker = os.path.join(self.tmp, "marker")
        evil = os.path.join(self.tmp, "setup_dependencies.py")
        with open(evil, "w") as fd:
            fd.write("import os\nos.system('touch %s')\nopen(%r, 'w').close()\n"
                     "dependencies = {'wmagent': {'packages': ['WMComponent+']}}\n" % (marker, marker))
        self.assertEqual(IT.load_dependencies(evil), {"wmagent": {"packages": ["WMComponent+"]}})
        self.assertFalse(os.path.exists(marker))

    def test_files_fake_scenarios(self):
        run_main(check_argv(self.work, scenario="not-patchable"))
        code, stdout, _ = run_main(["files", "--work", self.work, "--pr-files", self.pr_files])
        self.assertEqual(code, 0)
        res = last_json(stdout)
        self.assertFalse(res["go"])
        self.assertEqual(res["source"], "fixture")
        self.assertEqual(len(res["files"]), 4)
        self.assertEqual(res["real_files"][0]["path"], "src/python/WMCore/WorkQueue/WorkQueue.py")
        self.assertEqual(self.state()["stop"]["verdict"], "not-patchable")
        shutil.rmtree(self.work)
        run_main(check_argv(self.work, scenario="pass"))
        res = last_json(run_main(["files", "--work", self.work, "--pr-files", self.pr_files])[1])
        self.assertTrue(res["go"])
        self.assertEqual([f["path"] for f in res["files"]],
                         ["src/python/WMComponent/JobAccountant/AccountantWorker.py"])


# ---------------------------------------------------------------------------
class InjectTest(Base):

    def test_argv(self):
        argv = IT.build_inject_argv("fake", "GHA_INJ_PR12_abc1234_R123", "/repo")
        self.assertEqual(argv, [sys.executable, "/repo/test/data/ReqMgr/inject-test-wfs.py",
                                "-u", "https://cmsweb-testbed.cern.ch", "-m", "GHA", "-f", "SC_ProdPsi_small.json",
                                "-c", "GHA_INJ_POC", "-r", "GHA_INJ_PR12_abc1234_R123", "-t", "testbed-vocms0263",
                                "-s", "T2_CH_CERN", "-a", "DMWM_TEST", "-p", "GHA_INJ", "-v", "1", "--dryRun"])
        only = IT.build_inject_argv("inject-only", "x", "/repo")
        self.assertNotIn("--dryRun", only)
        self.assertIn("--injectOnly", only)
        assign = IT.build_inject_argv("assign", "x", "/repo")
        self.assertNotIn("--dryRun", assign)
        self.assertNotIn("--injectOnly", assign)

    def test_child_env_and_result(self):
        parent = {"X509_USER_CERT": "/real/usercert.pem", "X509_USER_KEY": "/real/userkey.pem",
                  "GH_TOKEN": "ghp_" + "a" * 36, "GITHUB_TOKEN": "x"}
        with mock.patch.dict(os.environ, parent):
            out = self.pipeline(until="inject")
        self.assertEqual(out["inject"][0], 0)
        res = out["inject"][1]
        self.assertTrue(res["ok"] and res["dry_run"])
        env = self.calls[0]["env"]
        for var in ("X509_USER_CERT", "X509_USER_KEY", "X509_USER_PROXY"):
            self.assertEqual(env[var], "/nonexistent")
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("GITHUB_TOKEN", env)
        sandbox = self.calls[0]["cwd"]
        template = os.path.join(sandbox, "WMCore/test/data/ReqMgr/requests/GHA/SC_ProdPsi_small.json")
        self.assertTrue(os.path.isfile(template) and not os.path.islink(template))
        found = [f for _, _, files in os.walk(sandbox) for f in files if f == "reqmgr2.py"]
        self.assertEqual(found, [])
        self.assertTrue(self.state()["inject"]["started"])

    def test_mode_assign_refused(self):
        self.pipeline(until="files")
        state = self.state()
        state["run"]["mode"] = "assign"
        self.write_state(state)
        code, _, _ = run_main(["inject", "--work", self.work, "--repo-root", REPO])
        self.assertEqual(code, 3)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.state()["stop"]["verdict"], "error")

    def test_dry_run_guard(self):
        self.pipeline(until="files")
        real = IT.build_inject_argv
        with mock.patch.object(IT, "build_inject_argv", lambda *a: [x for x in real(*a) if x != "--dryRun"]):
            code, _, err = run_main(["inject", "--work", self.work, "--repo-root", REPO])
        self.assertEqual(code, 1)
        self.assertIn("AssertionError", err)
        self.assertEqual(self.calls, [])
        run_main(["report", "--work", self.work, "--out", os.path.join(self.tmp, "out")])
        _, status, _ = self.outputs()
        self.assertEqual(status["verdict"], "error")
        self.assertIn("inject", status["description"])

    def test_sandbox_not_built(self):
        self.pipeline(until="files")

        def broken(work, fixtures):
            sandbox = os.path.join(work, "sandbox")
            os.makedirs(os.path.join(sandbox, "WMCore"), exist_ok=True)
            return sandbox
        with mock.patch.object(IT, "build_sandbox", broken):
            code, stdout, _ = run_main(["inject", "--work", self.work, "--repo-root", REPO])
        self.assertEqual(code, 0)
        self.assertEqual(last_json(stdout)["reasons"], ["sandbox not built"])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.state()["stop"]["verdict"], "error")

    def test_clone_detection(self):
        cases = [dict(log_lines=GOOD_LOG[1:]),
                 dict(log_lines=GOOD_LOG + ["2026-10-06 12:00:00,000:INFO:x: WMCore repository successfully cloned!"]),
                 dict(make_git=True)]
        for kwargs in cases:
            shutil.rmtree(self.work, ignore_errors=True)
            self.stub_child(**kwargs)
            out = self.pipeline(until="inject")
            self.assertEqual(out["inject"][1]["reasons"], ["the script tried to clone WMCore"], kwargs)
            self.assertEqual(self.state()["stop"]["verdict"], "error")

    def test_tweaked_mismatch(self):
        self.stub_child(tweak=lambda d: d["assignRequest"].update({"Team": "other"}))
        out = self.pipeline(until="inject")
        self.assertEqual(out["inject"][1]["reasons"], ["tweaked template has the wrong Team"])

    def test_wait_uses_inject_time(self):
        # F4: deadline and fake transitions start at the inject time stored in the state
        self.pipeline(until="inject")
        state = self.state()
        state["inject"]["time"] = 1791288000
        self.write_state(state)
        code, stdout, _ = run_main(["wait", "--work", self.work])
        self.assertEqual(code, 0)
        res = last_json(stdout)
        self.assertEqual(res["deadline_at"], 1791288000 + 5 * 3600)
        self.assertEqual(res["timeline"][0], ["new", 0])

    def test_wait_mode_guard(self):
        self.pipeline(until="inject")
        state = self.state()
        state["run"]["mode"] = "readonly"
        self.write_state(state)
        code, _, _ = run_main(["wait", "--work", self.work])
        self.assertEqual(code, 3)
        self.assertEqual(self.state()["stop"]["reasons"], ["mode readonly not built in M1a"])

    def test_request_name_format(self):
        name = IT.invent_request_name("gha_fake", "SC_ProdPsi_small_GHA_INJ_PR12_abc1234_R123", 1791288000)
        self.assertRegex(name, r"^gha_fake_SC_ProdPsi_small_GHA_INJ_PR12_abc1234_R123_261006_120000_\d{4}$")
        self.assertLessEqual(len(name), 150)


@unittest.skipUnless(os.path.isfile(os.path.join(REPO, "test/data/ReqMgr/inject-test-wfs.py")),
                     "repository root not found")
class RealDryRunTest(Base):
    """The real inject-test-wfs.py, --dryRun, in the temp sandbox. Takes about 5 s (the script sleeps)."""

    def setUp(self):
        super().setUp()
        for p in self.patches:
            p.stop()
        self.patches = []

    def test_real_dry_run(self):
        out = self.pipeline(until="files")
        with no_network():
            code, stdout, err = run_main(["inject", "--work", self.work, "--repo-root", REPO])
        self.assertEqual(code, 0, stdout + err)
        res = last_json(stdout)
        self.assertTrue(res["ok"], res)
        with open(os.path.join(self.work, "inject.log")) as fd:
            self.assertIn("dry-run command:", fd.read())
        with open(IT.tweaked_json_path()) as fd:
            tweaked = json.load(fd)
        self.assertEqual(tweaked["createRequest"]["Campaign"], "GHA_INJ_POC")
        self.assertEqual(tweaked["createRequest"]["RequestString"], "SC_ProdPsi_small_GHA_INJ_PR12_abc1234_R123")
        self.assertEqual(tweaked["assignRequest"]["Team"], "testbed-vocms0263")
        self.assertEqual(tweaked["assignRequest"]["SiteWhitelist"], ["T2_CH_CERN"])
        self.assertNotIn(IT.tweaked_json_path(), stdout + err)
        self.assertNotIn("x509up", stdout + err)
        self.assertTrue(out["check"][1]["go"])


class ParseRequestNameTest(unittest.TestCase):

    def test_parse(self):
        log = ("2026-10-06 12:00:00,000:INFO:reqmgr2: Approving request 'x_y_261006_120000_1234' ...\n"
               "2026-10-06 12:00:00,000:INFO:reqmgr2: Create request 'x_y_261006_120000_1234' succeeded.\n")
        self.assertEqual(IT.parse_request_name(log), "x_y_261006_120000_1234")
        self.assertIsNone(IT.parse_request_name("nothing"))
        two = "Create request 'a' succeeded.\nCreate request 'b' succeeded.\n"
        self.assertIsNone(IT.parse_request_name(two))
        self.assertIsNone(IT.parse_request_name("Approving request 'x' ..."))
        self.assertIsNone(IT.parse_request_name("Create request 'a b' succeeded."))
        self.assertIsNone(IT.parse_request_name("Create request 'a'b' succeeded."))


# ---------------------------------------------------------------------------
class JobCountsTest(unittest.TestCase):

    def answer(self, *statuses, with_info=True):
        doc = {"RequestName": "r"}
        if with_info:
            doc["AgentJobInfo"] = {"agent%d" % i: {"status": s} for i, s in enumerate(statuses)}
        return {"result": [{"r": doc}]}

    def test_counts(self):
        with open(os.path.join(FIXTURES, "wmstats_request.json")) as fd:
            jobs = IT.job_counts(json.load(fd))
        self.assertEqual((jobs["success"], jobs["failure"]), (6, 0))
        self.assertEqual(IT.job_counts(self.answer({"failure": {"exception": 1, "submit": 1}}))["failure"], 2)
        self.assertEqual(IT.job_counts(self.answer({"cooloff": 3}))["cooloff"], 3)
        self.assertEqual(IT.job_counts(self.answer({"cooloff": {"create": 1, "job": 2}}))["cooloff"], 3)
        two = IT.job_counts(self.answer({"success": 2, "submitted": {"running": 1}},
                                        {"success": 3, "submitted": {"pending": 4}}))
        self.assertEqual((two["success"], two["running"], two["pending"]), (5, 1, 4))
        self.assertEqual(set(IT.job_counts(self.answer(with_info=False)).values()), {0})


class DecideTest(unittest.TestCase):

    def d(self, status, success=0, failure=0, dbs=0, psc=0, deadline=False):
        return IT.decide(status, {"success": success, "failure": failure}, dbs, psc, deadline)

    def test_rules(self):
        self.assertEqual(self.d("completed", 2, 0, 1)[0], "pass")
        self.assertEqual(self.d("completed", 6, 0, 0, 0)[0], "running")
        self.assertEqual(self.d("completed", 6, 0, 0, IT.GRACE_POLLS), ("fail", ["no new file in DBS int"]))
        self.assertEqual(self.d("completed", 6, 0, 0, 0, deadline=True)[0], "fail")
        for status in ("closed-out", "announced", "normal-archived"):
            self.assertEqual(self.d(status, 2, 0, 1)[0], "pass")
        self.assertEqual(self.d("force-complete", 2, 0, 1)[0], "running")
        self.assertEqual(self.d("completed", 1, 0, 2, IT.GRACE_POLLS), ("fail", ["only 1 successful jobs"]))
        self.assertEqual(self.d("running-open", 0, 1), ("fail", ["1 failed job"]))
        for status in ("failed", "aborted", "rejected", "aborted-archived"):
            self.assertEqual(self.d(status)[0], "fail")
        self.assertEqual(self.d("acquired", deadline=True), ("timeout", ["still acquired after 5 h"]))
        self.assertEqual(self.d("running-open")[0], "running")
        self.assertEqual(self.d("acquired", 0, 2, deadline=True)[0], "fail")


class WaitTest(unittest.TestCase):

    def run_scenario(self, scenario, deadline_s=5 * 3600, **kw):
        clock = IT.FakeClock(1791288000)
        source = IT.FakeSource(FIXTURES, scenario, "req_x", clock, **kw)
        with mock.patch.object(IT, "HOP_S", 0):
            return IT.run_wait(source, clock, "req_x", deadline_s, 15 * 60), source

    def test_pass(self):
        res, _ = self.run_scenario("pass")
        self.assertEqual((res["verdict"], res["polls"], res["elapsed_s"]), ("pass", 10, 135 * 60))
        self.assertEqual((res["dbs_files"], res["dbs_events"]), (2, 200))
        self.assertEqual([s for s, _ in res["timeline"]],
                         ["new", "assignment-approved", "assigned", "staging", "staged", "acquired",
                          "running-open", "running-closed", "completed"])

    def test_fail(self):
        res, _ = self.run_scenario("fail")
        self.assertEqual((res["verdict"], res["polls"], res["reasons"]), ("fail", 6, ["1 failed job"]))

    def test_timeout(self):
        res, _ = self.run_scenario("timeout")
        self.assertEqual((res["verdict"], res["polls"], res["final_status"]), ("timeout", 21, "acquired"))

    def test_short_deadline(self):
        res, _ = self.run_scenario("timeout", deadline_s=0.5 * 3600)
        self.assertEqual((res["verdict"], res["polls"], res["elapsed_s"]), ("timeout", 3, 1800))

    def test_old_dbs_files_not_counted(self):
        res, _ = self.run_scenario("pass", dbs_creation_date=0)
        self.assertEqual((res["verdict"], res["dbs_files"]), ("fail", 0))
        self.assertEqual(res["reasons"], ["no new file in DBS int"])

    def test_dbs_files(self):
        source = IT.FakeSource(FIXTURES, "pass", "req_x", IT.FakeClock(0))
        datasets = source.request_doc(9)["OutputDatasets"]
        counts = [len(source.dbs_files(ds, 9)) for ds in datasets]
        self.assertEqual(counts, [2, 0, 0])
        source.timeline = [("completed", {}, 5)]
        with self.assertRaises(AssertionError):
            source.dbs_files(datasets[0], 0)

    def test_no_job_info_while_acquired(self):
        source = IT.FakeSource(FIXTURES, "pass", "req_x", IT.FakeClock(0))
        self.assertEqual(source.request_doc(3)["RequestStatus"], "acquired")
        doc = source.wmstats_answer(3)["result"][0]["req_x"]
        self.assertNotIn("AgentJobInfo", doc)
        self.assertIn("AgentJobInfo", source.wmstats_answer(4)["result"][0]["req_x"])

    def test_deadline_counts_from_t0(self):
        # F4: a t0 one hour before the clock start leaves 4 h of polling
        clock = IT.FakeClock(1791288000 + 3600)
        source = IT.FakeSource(FIXTURES, "timeout", "req_x", clock)
        with mock.patch.object(IT, "HOP_S", 0):
            res = IT.run_wait(source, clock, "req_x", 5 * 3600, 15 * 60, t0=1791288000)
        self.assertEqual((res["verdict"], res["elapsed_s"], res["polls"]), ("timeout", 5 * 3600, 17))

    def test_env_overrides(self):
        tmp = tempfile.mkdtemp()
        try:
            for env in ({"WMCI_INJECT_SITE": "bad"}, {"WMCI_INJECT_DEADLINE_H": "6"},
                        {"WMCI_INJECT_POLL_MIN": "0"}, {"WMCI_INJECT_TEAM": "a b"}):
                code, _, err = run_main(check_argv(os.path.join(tmp, "w")), env=env)
                self.assertEqual(code, 2, env)
                self.assertNotIn("bad", err.replace("WMCI", ""))
        finally:
            shutil.rmtree(tmp)


# ---------------------------------------------------------------------------
class CleanupTest(Base):

    def test_actions(self):
        for status in ("completed", "new", "assignment-approved", "failed", "closed-out"):
            self.assertEqual(IT.cleanup_action(status)[0], "reject", status)
        for status in ("assigned", "staging", "staged", "acquired", "running-open", "running-closed"):
            self.assertEqual(IT.cleanup_action(status)[0], "abort", status)
        self.assertEqual(IT.cleanup_action("force-complete"), ("none", "reject by hand after completed"))
        for status in ("rejected", "aborted-archived"):
            self.assertEqual(IT.cleanup_action(status)[0], "none")

    def test_actions_follow_request_status(self):
        path = os.path.join(REPO, "src/python/WMCore/ReqMgr/DataStructs/RequestStatus.py")
        with open(path) as fd:
            tree = ast.parse(fd.read())
        start, trans = None, None
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                if node.targets[0].id == "REQUEST_START_STATE":
                    start = ast.literal_eval(node.value)
                elif node.targets[0].id == "REQUEST_STATE_TRANSITION":
                    trans = node.value

        class Swap(ast.NodeTransformer):
            def visit_Name(self, node):
                if node.id == "REQUEST_START_STATE":
                    return ast.copy_location(ast.Constant(start), node)
                return node
        transitions = ast.literal_eval(Swap().visit(trans))
        for status, allowed in transitions.items():
            action = IT.cleanup_action(status)[0]
            if action == "reject":
                self.assertIn("rejected", allowed, status)
            if action == "abort":
                self.assertIn("aborted", allowed, status)
        self.assertEqual(IT.REJECT_FROM | IT.ABORT_FROM, IT.REJECT_FROM.union(IT.ABORT_FROM) & set(transitions))

    def test_cleanup_fake(self):
        out = self.pipeline(until="cleanup")
        res = out["cleanup"][1]
        self.assertFalse(res["sent"])
        name = out["inject"][1]["request"]
        self.assertEqual(res["would_send"], 'PUT /reqmgr2/data/request/%s {"RequestStatus": "rejected"}' % name)
        self.assertEqual(IT.cleanup_result({"inject": {"started": False}})["action"], "none")
        self.assertEqual(IT.cleanup_result({"inject": {"started": True}})["action"], "unknown")
        self.assertEqual(IT.cleanup_result(None), {"action": "none", "note": "no state"})


class RedactTest(unittest.TestCase):

    def test_redact(self):
        self.assertEqual(IT.redact("2026:INFO:x: Identity files:"), "[line hidden: credentials]")
        self.assertEqual(IT.redact("\tcert file: '/x/y'\n\tkey file:  '/x/z'"),
                         "[line hidden: credentials]\n[line hidden: credentials]")
        self.assertEqual(IT.redact("wrote /tmp/vkhlaisu.json"), "wrote <tmp-json>")
        self.assertEqual(IT.redact("/tmp/x509up_u1000"), "<proxy>")
        out = IT.redact("cert /srv/runner/grid-secrets/usercert.pem here")
        self.assertEqual(out, "cert <secret-path> here")
        for part in ("srv", "runner", "grid-secrets", "usercert", ".pem"):
            self.assertNotIn(part, out)
        tmp = tempfile.mkdtemp()
        try:
            with mock.patch.dict(os.environ, {"X509_USER_KEY": os.path.join(tmp, "k")}):
                self.assertNotIn(tmp, IT.redact("key at %s/k" % tmp))
        finally:
            shutil.rmtree(tmp)
        self.assertEqual(IT.redact('"DN": "%s"' % DN), '"DN": "<dn>"')
        self.assertEqual(IT.redact("token ghp_" + "a" * 36), "token <token>")
        self.assertEqual(IT.redact("Authorization: Bearer abc"), "Authorization: <token>")
        plain = "gha_fake_SC_ProdPsi_small_GHA_INJ_PR0_0000000_R0_261006_120000_1234 " \
                "/RelValPsi2SToJPsiPiPi/CMSSW_12_0_0-GenSimFull_SC_ProdPsi_small_GHA_INJ-v1/GEN-SIM"
        self.assertEqual(IT.redact(plain), plain)

    def test_secret_folder_forms(self):
        # S2 and F1 (spec 2.10 rule 2): any token containing ci-secrets, any path through a *secrets folder
        for text, want in (("(/srv/x/ci-secrets)", "(<secret-path>)"),
                           ("from /srv/x/ci-secrets.", "from <secret-path>"),
                           ("/srv/x/ci-secrets, ok", "<secret-path>, ok"),
                           ("dir /srv/x/ci-secrets; done", "dir <secret-path>; done"),
                           ("WMCI_SECRETS_DIR:/srv/x/ci-secrets:x", "<secret-path>"),
                           ("path=ci-secrets/proxy.txt", "<secret-path>"),
                           ("ci-secrets/rucio_account.txt", "<secret-path>"),
                           ("read grid-secrets/proxy now", "read <secret-path> now"),
                           ("(/srv/grid-secrets/a)", "(<secret-path>)")):
            self.assertEqual(IT.redact(text), want, text)
        self.assertEqual(IT.redact("no secrets in this job"), "no secrets in this job")

    def test_data_home(self):
        # F1: /data/<runner user> (built at run time, never written in the code)
        with mock.patch.object(IT, "_data_home", lambda: "/data/runneruser"):
            self.assertEqual(IT.redact("dir /data/runneruser/rucio/cfg"), "dir <secret-path>")
            self.assertEqual(IT.redact("(/data/runneruser)."), "(<secret-path>).")
            self.assertEqual(IT.redact("/data/runneruser2/x"), "/data/runneruser2/x")
        home = IT._data_home()
        if home:
            self.assertEqual(IT.redact(home + "/actions-runner/_work"), "<secret-path>")

    def test_runner_paths(self):
        # S1: runner folders by value, longest first
        env = {"GITHUB_WORKSPACE": "/srv/gh/runner/_work/WMCore/WMCore", "RUNNER_WORKSPACE": "/srv/gh/runner/_work/WMCore",
               "RUNNER_TEMP": "/srv/gh/runner/_work/_temp", "HOME": "/srv/gh"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(IT.redact("open /srv/gh/runner/_work/WMCore/WMCore/wmcore-src/a.py"),
                             "open <runner>/wmcore-src/a.py")
            self.assertEqual(IT.redact("at /srv/gh/runner/_work/_temp/w"), "at <runner>/w")
            self.assertEqual(IT.redact("~ is /srv/gh"), "~ is <runner>")
        with mock.patch.dict(os.environ, {"HOME": "/"}):
            self.assertEqual(IT.redact("/a/b"), "/a/b")

    def test_redact_obj_drops_keys(self):
        obj = IT.redact_obj({"DN": DN, "a": [{"RequestorDN": DN, "b": "ok"}], "proxy_warning": "x"})
        self.assertEqual(obj, {"a": [{"b": "ok"}]})


# ---------------------------------------------------------------------------
class ReportTest(Base):

    def scenario_outputs(self, scenario):
        self.pipeline(scenario)
        return self.outputs()

    def test_scenarios(self):
        expect = {"pass": ("PASS", "success", "FAKE: PASS: completed, 6 jobs ok, 2 new files in DBS int"),
                  "fail": ("FAIL", "failure", "FAKE: FAIL: 1 failed job"),
                  "timeout": ("TIMEOUT", "error", "FAKE: TIMEOUT: still acquired after 5 h"),
                  "busy": ("NOT STARTED: testbed busy", "error",
                           "FAKE: NOT STARTED: testbed busy (1 active GHA_INJ_POC request)"),
                  "not-patchable": ("NOT STARTED: not patchable", "error",
                                    "FAKE: NOT STARTED: PR cannot be put on the agent "
                                    "(setup.py: no patchComponent.sh destination)")}
        for scenario, (title, state, desc) in expect.items():
            shutil.rmtree(self.work, ignore_errors=True)
            comment, status, state_text = self.scenario_outputs(scenario)
            self.assertTrue(comment.startswith(IT.MARKER + "\n"), scenario)
            self.assertEqual(comment.count(IT.MARKER), 1)
            self.assertIn("### Injection test: %s\n" % title, comment)
            self.assertIn("**FAKE MODE**", comment)
            self.assertIn("The code of this PR was NOT deployed.", comment)
            self.assertEqual((status["state"], status["description"]), (state, desc), scenario)
            self.assertLessEqual(len(status["description"]), 140)
            for text in (comment, json.dumps(status), state_text):
                for bad in ("CN=", "DC=", "/tmp/", "-secrets", "usercert", "userkey", "x509up"):
                    self.assertNotIn(bad, text, (scenario, bad))
            self.assertNotIn("dry-run command", state_text)

    def test_pass_table_rows(self):
        # F2: the spec 2.11 rows and the status timeline
        comment, _, _ = self.scenario_outputs("pass")
        lines = comment.splitlines()
        for row in ("| Jobs | success 6, failure 0, cooloff 0, pending 0, running 0 |",
                    "| DBS int | 2 new files, 200 events |",
                    "| Waited | 2 h 15 min, 10 polls (fake clock) |",
                    "| Cleanup | reject from `completed` (fake mode: not sent) |",
                    "| Final status | `completed` |", "| Mode | `fake` |", "| PR / commit | #12 at `abc1234` |"):
            self.assertIn(row, lines)
        start = lines.index("| status | minutes after injection |") + 2
        timeline = [l for l in lines[start:] if l.startswith("| `")][:9]
        self.assertEqual(timeline, ["| `%s` | %d |" % sm for sm in (
            ("new", 0), ("assignment-approved", 0), ("assigned", 0), ("staging", 15), ("staged", 30),
            ("acquired", 45), ("running-open", 60), ("running-closed", 105), ("completed", 120))])
        self.assertIn("- completed with 6 successful jobs, 0 failed, 2 new files in DBS int", lines)

    def test_crash_path_not_published(self):
        # S1: a crash message with a runner path never reaches comment, status or state.json
        ws = os.path.join(self.tmp, "gh-runner", "_work", "WMCore", "WMCore")
        env = {"GITHUB_WORKSPACE": ws, "RUNNER_TEMP": os.path.join(self.tmp, "gh-runner", "_work", "_temp")}
        with mock.patch.dict(os.environ, env):
            self.pipeline(until="inject")
            state = self.state()
            state["run"]["fixtures"] = os.path.join(ws, "wmcore-src", ".github", "ci", "inject", "gone")
            self.write_state(state)
            code, _, err = run_main(["wait", "--work", self.work])
            self.assertEqual(code, 1)
            self.assertIn("FileNotFoundError", err)
            self.assertIn("<runner>/wmcore-src", self.state()["wait"]["error"])
            run_main(["report", "--work", self.work, "--out", os.path.join(self.tmp, "out")])
        comment, status, state_text = self.outputs()
        self.assertIn("(runner)/wmcore-src", comment)   # plain() turns <> into ()
        for text in (comment, json.dumps(status), state_text):
            self.assertNotIn("gh-runner", text)
            self.assertNotIn(self.tmp, text)

    def test_comment_cut(self):
        # F3: the MAX_COMMENT cut
        state = {"run": {"mode": "fake"}, "check": {"done": True},
                 "stop": {"stage": "check", "verdict": "error", "reasons": ["r" * 2500] * 30}}
        comment = IT.render_comment(state)
        self.assertLessEqual(len(comment), IT.MAX_COMMENT)
        self.assertGreater(len(comment), IT.MAX_COMMENT - 100)
        self.assertTrue(comment.endswith("\n\n(comment cut: too long)\n"))

    def test_error_and_agent_titles(self):
        state = {"run": {"mode": "fake", "pr": "1", "sha7": "abc1234", "login": "alice", "scenario": "pass"},
                 "check": {"done": True}, "files": {"done": True}, "inject": {"done": False}}
        self.assertIn("### Injection test: ERROR\n", IT.render_comment(state))
        status = IT.status_for(state, IT.final_verdict(state))
        self.assertEqual(status["description"], "FAKE: ERROR in inject: stage inject did not finish")
        state["stop"] = {"stage": "check", "verdict": "error", "reasons": ["agent x is in drain mode"],
                         "kind": "agent"}
        self.assertIn("### Injection test: NOT STARTED: agent not ready\n", IT.render_comment(state))

    def test_stop_wins_over_not_done(self):
        state = {"run": {"mode": "fake"}, "check": {"done": True}, "files": {"done": False},
                 "stop": {"stage": "files", "verdict": "not-patchable", "reasons": ["x"]}}
        self.assertEqual(IT.final_verdict(state)["verdict"], "not-patchable")

    def test_no_state(self):
        os.makedirs(self.work)
        code, stdout, _ = run_main(["report", "--work", self.work, "--out", os.path.join(self.tmp, "out")])
        self.assertEqual(code, 0)
        _, status, _ = self.outputs()
        self.assertEqual(status["verdict"], "error")
        self.assertIn("no state: pre-flight did not start", status["description"])
        code, stdout, _ = run_main(["cleanup", "--work", self.work])
        self.assertEqual(last_json(stdout)["action"], "none")

    def pass_state(self):
        self.pipeline("pass", until="cleanup")
        return self.state()

    def test_hostile_file_names(self):
        state = self.pass_state()
        state["files"]["real_files"] = [
            {"path": "a\n### Injection test: PASS", "scope": "NONE", "patchable": False, "why": "x"},
            {"path": "x@someone<!--|y`z", "scope": "NONE", "patchable": False, "why": "x"},
            {"path": "b" * 500, "scope": "NONE", "patchable": False, "why": "x"}]
        comment = IT.render_comment(state)
        self.assertEqual(len([l for l in comment.splitlines() if l.startswith("### ")]), 1)
        row = [l for l in comment.splitlines() if "someone" in l][0]
        self.assertIn("`x@someone<!--\\|yz`", row)
        self.assertEqual(len(re.split(r"(?<!\\)\|", row)), 6)   # 4 cells
        self.assertIn("`" + "b" * 200 + "...`", comment)
        self.assertNotIn("b" * 201, comment)

    def test_many_files(self):
        state = self.pass_state()
        state["files"]["real_files"] = [{"path": "src/python/f%d.py" % i, "scope": "NONE", "patchable": True,
                                         "why": ""} for i in range(400)]
        comment = IT.render_comment(state)
        self.assertIn("and 350 more", comment)
        self.assertLess(len(comment), 60000)

    def test_log_tail(self):
        log = "\n".join(["2026-10-06 12:00:00,000:INFO:reqmgr2: Identity files:",
                         "\tcert file: '/x/usercert.pem'",
                         "Traceback (most recent call last):",
                         "2026-10-06 12:00:00,000:ERROR:reqmgr2: " + "e" * 300])
        many = "\n".join("2026-10-06 12:00:00,000:INFO:x: line %d" % i for i in range(25))
        tail = IT.log_tail(many)
        self.assertEqual(len(tail), 20)
        self.assertEqual((tail[0], tail[-1]), ("2026-10-06 12:00:00,000:INFO:x: line 5",
                                               "2026-10-06 12:00:00,000:INFO:x: line 24"))
        tail = IT.log_tail(log)
        self.assertEqual(len(tail), 2)
        self.assertEqual(tail[0], "[line hidden: credentials]")
        self.assertTrue(all(len(l) <= 200 for l in tail))
        state = {"run": {"mode": "fake"}, "check": {"done": True}, "files": {"done": True},
                 "inject": {"done": True}, "stop": {"stage": "inject", "verdict": "error", "reasons": ["x"]}}
        comment = IT.render_comment(state, log)
        self.assertIn("Inject log", comment)
        self.assertNotIn("usercert", comment)
        self.assertNotIn("Traceback", comment)

    def test_warning_in_real_mode(self):
        state = self.pass_state()
        state["run"]["mode"] = "assign"
        self.assertIn("**WARNING:** request", IT.render_comment(state))
        state["run"]["mode"] = "fake"
        self.assertNotIn("**WARNING:**", IT.render_comment(state))


# ---------------------------------------------------------------------------
class EndToEndTest(Base):

    def test_pass(self):
        out = self.pipeline("pass")
        self.assertTrue(out["check"][1]["go"] and out["files"][1]["go"] and out["inject"][1]["ok"])
        self.assertEqual(out["wait"][1]["verdict"], "pass")
        self.assertEqual(out["cleanup"][1]["action"], "reject")
        self.assertEqual(out["report"][1]["state"], "success")
        self.assertTrue(all(code == 0 for code, _ in out.values()))

    def test_fail(self):
        out = self.pipeline("fail")
        self.assertEqual(out["report"][1]["state"], "failure")
        self.assertEqual((out["cleanup"][1]["action"], out["cleanup"][1]["from_status"]), ("abort", "running-open"))

    def test_timeout(self):
        out = self.pipeline("timeout")
        self.assertEqual(out["report"][1]["state"], "error")
        self.assertEqual((out["cleanup"][1]["action"], out["cleanup"][1]["from_status"]), ("abort", "acquired"))

    def test_busy(self):
        out = self.pipeline("busy")
        self.assertFalse(out["check"][1]["go"])
        for stage in ("files", "inject", "wait"):
            self.assertTrue(out[stage][1]["skipped"], stage)
        self.assertEqual(out["cleanup"][1]["action"], "none")
        self.assertEqual(out["report"][1]["state"], "error")
        self.assertEqual(self.calls, [])

    def test_not_patchable(self):
        out = self.pipeline("not-patchable")
        self.assertFalse(out["files"][1]["go"])
        self.assertTrue(out["inject"][1]["skipped"])
        self.assertEqual(out["report"][1]["verdict"], "not-patchable")

    def test_uploaded_state(self):
        self.pipeline("pass")
        _, _, state_text = self.outputs()
        self.assertNotIn("CN=", state_text)
        self.assertNotIn("dry-run command", state_text)
        self.assertNotIn('"DN"', state_text)


# ---------------------------------------------------------------------------
@unittest.skipUnless(shutil.which("bash") and os.path.isfile(AGENT_SH), "bash or agent.sh missing")
class AgentShTest(unittest.TestCase):

    def sh(self, *args, env=None):
        environ = {k: v for k, v in os.environ.items() if k != "WMCI_INJECT_PATCH"}
        environ.update(env or {})
        return subprocess.run(["bash", AGENT_SH] + list(args), capture_output=True, text=True, env=environ,
                              check=False)

    def test_usage(self):
        self.assertEqual(self.sh().returncode, 2)
        self.assertEqual(self.sh("explode").returncode, 2)
        self.assertEqual(self.sh("patch", "/nonexistent/file.diff").returncode, 2)
        self.assertEqual(self.sh("unpatch", "bad tag!").returncode, 2)

    def test_patch_guard(self):
        tmp = tempfile.mkdtemp()
        try:
            diff = os.path.join(tmp, "pr.diff")
            with open(diff, "w") as fd:
                fd.write("diff --git a/src/python/WMCore/A.py b/src/python/WMCore/A.py\n"
                         "--- a/src/python/WMCore/A.py\n+++ b/src/python/WMCore/A.py\n"
                         "@@ -1 +1 @@\n-old\n+SECRET_BODY_LINE\n")
            res = self.sh("patch", diff)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("nothing done", res.stdout + res.stderr)
            self.assertIn("src/python/WMCore/A.py", res.stdout + res.stderr)
            self.assertNotIn("SECRET_BODY_LINE", res.stdout + res.stderr)
            res = self.sh("patch", diff, env={"WMCI_INJECT_PATCH": "on"})
            self.assertEqual(res.returncode, 3)
            self.assertIn("not built in M1a", res.stdout + res.stderr)
        finally:
            shutil.rmtree(tmp)


# ---------------------------------------------------------------------------
class FixturesTest(unittest.TestCase):

    def test_json(self):
        for name in os.listdir(FIXTURES):
            if name.endswith(".json"):
                with open(os.path.join(FIXTURES, name)) as fd:
                    json.load(fd)

    def test_template_diff(self):
        def diff(a, b, path=""):
            if isinstance(a, dict) and isinstance(b, dict):
                out = set()
                for key in set(a) | set(b):
                    sub = path + "." + key if path else key
                    if key not in a or key not in b:
                        out.add(sub)
                    else:
                        out |= diff(a[key], b[key], sub)
                return out
            return set() if a == b else {path}
        with open(os.path.join(REPO, "test/data/ReqMgr/requests/DMWM/SC_ProdPsi.json")) as fd:
            original = json.load(fd)
        with open(os.path.join(FIXTURES, "SC_ProdPsi_small.json")) as fd:
            small = json.load(fd)
        self.assertEqual(diff(original, small), {
            "createRequest.Step1.RequestNumEvents", "createRequest.Step1.EventsPerJob",
            "createRequest.EnableHarvesting", "createRequest.DQMUploadUrl", "createRequest.Campaign",
            "assignRequest.CustodialSites", "assignRequest.NonCustodialSites"})
        self.assertEqual(small["createRequest"]["Step1"]["RequestNumEvents"], 200)
        self.assertEqual(small["createRequest"]["Step1"]["EventsPerJob"], 100)

    def test_module_rules(self):
        with open(MODULE_PATH) as fd:
            source = fd.read()
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                self.assertFalse(node.name.lower().startswith("test"), node.name)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertFalse(imported & {"socket", "http", "requests", "urllib"}, imported)
        self.assertNotIn("urlopen", source)
        self.assertNotIn("http.client", source)


if __name__ == "__main__":
    unittest.main()
