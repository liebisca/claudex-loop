"""Contract tests use real subprocesses and disposable Git repositories, no model calls."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("runner", ROOT / "skills/claudex-loop/scripts/runner.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)
SESSION = "12345678-1234-4567-8123-123456789abc"
GOOD = {"verdict": "APPROVED", "summary": "The supplied acceptance criteria are consistent.",
        "findings": [], "coverage": ["docs/custom plan.md"], "limitations": []}

FAKE_CLI = r'''
import json, os, pathlib, sys, time
if '--version' in sys.argv:
    print('fake-cli 1.0')
    sys.exit(0)
prompt = sys.stdin.read()
case = os.environ.get('FAKE_CASE', 'ok')
if case == 'timeout':
    time.sleep(30)
if case == 'active_timeout':
    while True:
        print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','name':'Read'}]}}), flush=True)
        time.sleep(0.05)
if case == 'exit':
    print('Authentication failed', file=sys.stderr)
    sys.exit(7)
if case == 'empty':
    sys.exit(0)
if case == 'mutate_plan':
    pathlib.Path(os.environ['FAKE_PLAN']).write_text('Changed after launch')
if case == 'mutate_code':
    pathlib.Path('new.py').write_text('changed during inspection')
if case == 'build':
    pathlib.Path('built.py').write_text('print(42)\n')
session = '12345678-1234-4567-8123-123456789abc'
if case == 'wrong_session':
    session = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
review = {'verdict':'APPROVED', 'summary':'Inspected supplied plan.',
          'findings':[], 'coverage':['custom plan.md'], 'limitations':[]}
if case == 'revise':
    review.update(verdict='REVISE', findings=[{'id':'R1','severity':'high','path':'plan',
                  'evidence':'Deletion before successful copy loses the only copy.',
                  'fix':'Verify the new copy before removing the old one.'}])
if case == 'blocked':
    review.update(verdict='BLOCKED', coverage=[], limitations=['Required schema unavailable.'])
if case == 'malformed':
    review = {'verdict':'APPROVED'}
if 'exec' in sys.argv:
    output = pathlib.Path(sys.argv[sys.argv.index('-o')+1])
    output.write_text('Built; proof passed.' if case == 'build' else json.dumps(review))
    print(json.dumps({'type':'thread.started', 'thread_id':session}))
    if case == 'separator':
        # Codex leaves U+0085/U+2028/U+2029 in command output unescaped.
        item = {'type':'command_execution', 'aggregated_output':'e.NEL="\x85",e.LS="\u2028",e.PS="\u2029"'}
        sys.stdout.flush()
        sys.stdout.buffer.write((json.dumps({'type':'item.completed', 'item':item},
                                            ensure_ascii=False) + '\n').encode('utf-8'))
        sys.stdout.buffer.flush()
    if case == 'turn_failed':
        print(json.dumps({'type':'turn.failed', 'error':{'message':'quota'}}))
    elif case != 'incomplete':
        print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':5}}))
else:
    value = {'type':'result','subtype':'success','is_error':False,'session_id':session,
             'structured_output':review, 'result':'Built; proof passed.',
             'modelUsage':{'claude-test':{'inputTokens':10}},'usage':{'input_tokens':10}}
    if case == 'turn_failed':
        value.update(subtype='error_during_execution',is_error=True)
    if case == 'array':
        print(json.dumps([{'type':'system','subtype':'init'}, value]))
    elif case == 'legacy':
        print(json.dumps(value))
    else:
        print(json.dumps({'type':'system','subtype':'init'}), flush=True)
        message = {'type':'assistant','message':{'model':'claude-reviewer',
                   'content':[{'type':'text','text':'line\u2028separator\u0085data\u2029'},
                              {'type':'tool_use','name':'Read','input':{'file_path':'fixture'}}]}}
        sys.stdout.buffer.write((json.dumps(message, ensure_ascii=False)+'\n').encode('utf-8'))
        sys.stdout.buffer.flush()
        if case == 'stream_malformed':
            print('not json')
        if case == 'stream_nonobject':
            print('42')
        if case == 'incomplete':
            sys.exit(0)
        print(json.dumps(value))
        if case == 'duplicate_result':
            print(json.dumps(value))
'''


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claudex-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "repo with spaces"
        self.repo.mkdir()
        self.plan = self.root / "custom plan.md"
        self.plan.write_text("# Work order\nKeep the original until the copy is verified.\n", encoding="utf-8")
        self.artifacts = self.root / "runs"
        self.cli = self.root / "fake_cli.py"
        self.cli.write_text(FAKE_CLI)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        (self.repo / "existing.py").write_text("original\n")
        (self.repo / "delete.py").write_text("delete me\n")
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")
        self.base = self.git("rev-parse", "HEAD").strip()

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo, stderr=subprocess.PIPE).decode()

    def invoke(self, host="claude", mode="review", case="ok", extra=()):
        args = [mode, "--host", host, "--repo", str(self.repo), "--plan", str(self.plan),
                "--artifacts", str(self.artifacts), *extra]
        old = set(self.artifacts.glob("*/result.json")) if self.artifacts.exists() else set()
        output, error = io.StringIO(), io.StringIO()
        with patch.object(runner, "cli_prefix", return_value=[sys.executable, str(self.cli)]), \
             patch.dict(os.environ, {"FAKE_CASE": case, "FAKE_PLAN": str(self.plan)}), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = runner.main(args)
        new = set(self.artifacts.glob("*/result.json")) - old if self.artifacts.exists() else set()
        path = next(iter(new)) if new else None
        return code, json.loads(path.read_text()) if path else None, path, error.getvalue()

    def test_host_role_defaults_and_builder_override(self):
        self.assertEqual(runner.resolve_roles("claude")["reviewer"], "codex")
        self.assertEqual(runner.resolve_roles("codex")["reviewer"], "claude")
        roles = runner.resolve_roles("codex", builder="claude")
        self.assertEqual((roles["planner"], roles["builder"], roles["inspector"]), ("codex", "claude", "codex"))
        with self.assertRaises(runner.RunError):
            runner.resolve_roles("codex", "codex")

    def test_both_review_adapters_complete_and_bind_custom_plan(self):
        for host in ("claude", "codex"):
            with self.subTest(host=host):
                code, record, path, _ = self.invoke(host)
                self.assertEqual(code, 0, record)
                self.assertEqual(record["session_id"], SESSION)
                self.assertEqual(record["plan"], str(self.plan))
                self.assertEqual(record["plan_sha256"], runner.digest(self.plan.read_bytes()))
                prompt = (path.parent / "prompt.txt").read_text()
                self.assertIn(str(self.plan), prompt)
                self.assertIn("repeatedly ask 'then what?'", prompt)
                self.assertIn("prefer leaving correct plans and code unchanged", prompt)
                self.assertEqual(record["response"]["verdict"], "APPROVED")

    def test_role_defaults_reach_cli_and_result(self):
        cases = (
            ("claude", "review", (), "gpt-6-astra", "high"),
            ("codex", "review", (), "claude-opus-5-5", "high"),
            ("claude", "inspect", (), "gpt-6-astra", "high"),
            ("codex", "inspect", (), "claude-opus-5-5", "high"),
            ("codex", "inspect", ("--builder", "claude"), "gpt-6-astra", "high"),
            ("claude", "inspect", ("--builder", "codex"), "claude-opus-5-5", "high"),
            ("claude", "build", (), "claude-sonnet-5", "high"),
            ("codex", "build", (), "gpt-6.1-sol", "high"),
            ("codex", "build", ("--builder", "claude"), "claude-sonnet-5", "high"),
            ("claude", "build", ("--builder", "codex"), "gpt-6.1-sol", "high"),
        )
        for host, mode, extra, model, effort in cases:
            with self.subTest(host=host, mode=mode, extra=extra):
                if mode == "inspect":
                    extra += ("--base", self.base)
                elif mode == "build":
                    extra += ("--unreviewed-spec", "--proof", "python check.py")
                code, record, path, _ = self.invoke(host, mode, extra=extra)
                self.assertEqual(code, 0, record)
                self.assertEqual((record["requested_model"], record["requested_effort"]), (model, effort))
                argv = json.loads((path.parent / "command.json").read_text())
                self.assertEqual(argv[argv.index("--model" if record["provider"] == "claude" else "-m") + 1], model)
                self.assertIn(effort if record["provider"] == "claude" else f'model_reasoning_effort="{effort}"', argv)

    def test_model_and_effort_overrides_are_independent(self):
        for host, default_model in (("claude", "gpt-6-astra"), ("codex", "claude-opus-5-5")):
            for extra, model, effort in (
                (("--model", "chosen-model"), "chosen-model", "high"),
                (("--effort", "medium"), default_model, "medium"),
                (("--model", "chosen-model", "--effort", "xhigh"), "chosen-model", "xhigh"),
            ):
                with self.subTest(host=host, extra=extra):
                    code, record, path, _ = self.invoke(host, extra=extra)
                    self.assertEqual(code, 0, record)
                    self.assertEqual((record["requested_model"], record["requested_effort"]), (model, effort))
                    argv = json.loads((path.parent / "command.json").read_text())
                    self.assertIn(model, argv)
                    self.assertIn(effort if record["provider"] == "claude" else f'model_reasoning_effort="{effort}"', argv)

    def test_resume_accepts_explicit_settings_matching_defaults(self):
        for host, model in (("claude", "gpt-6-astra"), ("codex", "claude-opus-5-5")):
            with self.subTest(host=host):
                _, _, previous, _ = self.invoke(host)
                code, record, _, _ = self.invoke(host, extra=("--resume", str(previous), "--model", model, "--effort", "high"))
                self.assertEqual(code, 0, record)
                self.assertEqual(record["session_id"], SESSION)

    def test_claude_exposes_only_read_tools_and_no_mcp(self):
        args = runner.command("claude", "review", self.root)
        self.assertEqual(args[args.index("--tools")+1], "Read,Glob,Grep")
        self.assertIn("--safe-mode", args)
        self.assertIn("--strict-mcp-config", args)
        self.assertEqual(args[args.index("--permission-mode")+1], "dontAsk")
        self.assertEqual(args[args.index("--output-format")+1], "stream-json")
        self.assertIn("--verbose", args)

    def test_codex_review_disables_mcp_config_but_build_retains_it(self):
        for mode in ("review", "inspect"):
            for session in (None, SESSION):
                self.assertIn("--ignore-user-config", runner.command("codex", mode, self.root, session=session))
        self.assertNotIn("--ignore-user-config", runner.command("codex", "build", self.root))

    def test_codex_resume_keeps_read_only_and_explicit_session(self):
        args = runner.command("codex", "review", self.root, session=SESSION)
        self.assertEqual(args[:3], ["exec", "resume", SESSION])
        self.assertIn('sandbox_mode="read-only"', args)
        self.assertNotIn("-s", args)
        self.assertNotIn("--last", args)

    def test_failures_never_approve_and_keep_diagnostics(self):
        for host in ("claude", "codex"):
            for case in ("exit", "empty", "malformed", "turn_failed"):
                with self.subTest(host=host, case=case):
                    code, record, path, _ = self.invoke(host, case=case)
                    self.assertEqual(code, 1)
                    self.assertEqual(record["status"], "failed")
                    self.assertTrue((path.parent / "stderr.txt").exists())
                    if case == "exit":
                        self.assertIn("Authentication failed", (path.parent / "stderr.txt").read_text())

    def test_missing_codex_completion_is_failure(self):
        code, record, _, _ = self.invoke(case="incomplete")
        self.assertEqual(code, 1)
        self.assertEqual(record["status"], "failed")

    def test_codex_unicode_line_separators_stay_inside_events(self):
        code, record, _, _ = self.invoke(case="separator")
        self.assertEqual(code, 0, record)
        self.assertEqual(record["response"]["verdict"], "APPROVED")

    def test_claude_array_envelope(self):
        code, record, _, _ = self.invoke("codex", case="array")
        self.assertEqual(code, 0)
        self.assertEqual(record["observed_models"], ["claude-test"])

    def test_claude_stream_tracks_tools_and_actual_reviewer_separately(self):
        code, record, path, _ = self.invoke("codex")
        self.assertEqual(code, 0, record)
        self.assertEqual(record["reviewer_models"], ["claude-reviewer"])
        self.assertEqual(record["observed_models"], ["claude-test"])
        progress = json.loads((path.parent / "progress.json").read_text())
        self.assertEqual(progress["events"], 3)
        self.assertEqual(progress["tool_calls"], 1)
        self.assertEqual(progress["last_event_type"], "result")

    def test_claude_legacy_result_remains_readable(self):
        code, record, _, _ = self.invoke("codex", case="legacy")
        self.assertEqual(code, 0, record)
        self.assertEqual(record["reviewer_models"], [])

    def test_claude_invalid_and_incomplete_streams_never_approve(self):
        for case in ("incomplete", "stream_malformed", "stream_nonobject", "duplicate_result"):
            with self.subTest(case=case):
                code, record, _, _ = self.invoke("codex", case=case)
                self.assertEqual(code, 1, record)
                self.assertEqual(record["status"], "failed")
                self.assertNotIn("response", record)

    def test_revise_and_blocked_are_completed_but_not_approval(self):
        for case in ("revise", "blocked"):
            code, record, _, _ = self.invoke(case=case)
            self.assertEqual(code, 0)
            with self.assertRaises(runner.RunError):
                runner.check_approval(record, self.plan, self.repo)

    def test_empty_findings_allowed_but_contradictory_approval_rejected(self):
        runner.validate_review(copy.deepcopy(GOOD))
        value = copy.deepcopy(GOOD)
        value["findings"] = [{"id":"1", "severity":"high", "path":"plan", "evidence":"data loss", "fix":"retain copy"}]
        with self.assertRaises(runner.RunError):
            runner.validate_review(value)

    def test_changed_plan_invalidates_approval(self):
        _, record, _, _ = self.invoke()
        runner.check_approval(record, self.plan, self.repo)
        self.plan.write_text("Different requirements")
        with self.assertRaises(runner.RunError):
            runner.check_approval(record, self.plan, self.repo)

    def test_changed_plan_during_review_fails(self):
        code, record, _, _ = self.invoke(case="mutate_plan")
        self.assertEqual(code, 1)
        self.assertIn("changed during", record["error"])

    def test_resume_revised_plan_same_session(self):
        _, _, previous, _ = self.invoke(case="revise")
        self.plan.write_text("New revision")
        code, record, _, _ = self.invoke(extra=("--resume", str(previous)))
        self.assertEqual(code, 0, record)
        self.assertEqual(record["session_id"], SESSION)

    def test_wrong_session_is_refused(self):
        _, _, previous, _ = self.invoke()
        code, record, _, _ = self.invoke(case="wrong_session", extra=("--resume", str(previous)))
        self.assertEqual(code, 1)
        self.assertIn("different session", record["error"])

    def test_resume_wrong_provider_or_model_rejected_before_launch(self):
        _, _, previous, _ = self.invoke()
        code, record, _, error = self.invoke("codex", extra=("--resume", str(previous)))
        self.assertEqual(code, 1)
        self.assertIsNone(record)
        self.assertIn("provider", error)
        code, record, _, error = self.invoke(extra=("--resume", str(previous), "--model", "new-model"))
        self.assertEqual(code, 1)
        self.assertIsNone(record)
        self.assertIn("requested_model", error)
        code, record, _, error = self.invoke(extra=("--resume", str(previous), "--effort", "max"))
        self.assertEqual(code, 1)
        self.assertIsNone(record)
        self.assertIn("requested_effort", error)

    def test_timeout_records_failure(self):
        code, record, _, _ = self.invoke(case="timeout", extra=("--timeout", "1"))
        self.assertEqual(code, 1)
        self.assertIn("timed out", record["error"])

    def test_active_events_do_not_reset_deadline(self):
        code, record, path, _ = self.invoke("codex", case="active_timeout", extra=("--timeout", "1"))
        self.assertEqual(code, 1, record)
        self.assertIn("timed out after 1s", record["error"])
        progress = json.loads((path.parent / "progress.json").read_text())
        self.assertGreater(progress["events"], 0)
        self.assertGreater(progress["tool_calls"], 0)
        self.assertLess(record["elapsed_seconds"], 5)

    def test_default_deadline_is_thirty_minutes(self):
        with patch.object(runner, "run", return_value=0) as run:
            self.assertEqual(runner.main(["review", "--host", "codex"]), 0)
        self.assertEqual(run.call_args.args[0].timeout, 1800)

    def test_large_stdin_and_outputs_do_not_deadlock_or_leak_in_progress(self):
        script = self.root / "large_io.py"
        script.write_text("import sys,json\n"
                          "sys.stderr.write('private stderr'*20000); sys.stderr.flush()\n"
                          "print(json.dumps({'type':'system','private':'PRIVATE REVIEW'*20000}),flush=True)\n"
                          "assert len(sys.stdin.read()) == 1000000\n")
        self.artifacts.mkdir()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = runner.execute([sys.executable, str(script)], "x" * 1000000,
                                  self.repo, self.artifacts, 5, heartbeat=0.05)
        self.assertEqual(code, 0)
        self.assertNotIn("PRIVATE REVIEW", output.getvalue())
        self.assertNotIn("private stderr", output.getvalue())
        progress = json.loads((self.artifacts / "progress.json").read_text())
        self.assertEqual(progress["events"], 1)
        self.assertGreater(progress["stderr_bytes"], 65536)

    @unittest.skipIf(os.name == "nt", "POSIX process-group cleanup; Windows uses taskkill /T")
    def test_timeout_stops_descendants(self):
        marker = self.root / "descendant-finished"
        script = self.root / "descendant.py"
        child = f"import time,pathlib; time.sleep(2); pathlib.Path({str(marker)!r}).touch()"
        script.write_text("import subprocess,sys,time\n"
                          f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
                          "print('{\"type\":\"system\"}',flush=True)\n"
                          "time.sleep(30)\n")
        self.artifacts.mkdir()
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(runner.RunError, "timed out"):
            runner.execute([sys.executable, str(script)], "prompt", self.repo, self.artifacts, 1)
        time.sleep(1.5)
        self.assertFalse(marker.exists())

    def test_progress_write_failure_stops_the_child(self):
        self.artifacts.mkdir()
        started = time.monotonic()
        with patch.object(runner, "save", side_effect=OSError("disk unavailable")), \
             self.assertRaisesRegex(OSError, "disk unavailable"):
            runner.execute([sys.executable, "-c", "import time; time.sleep(30)"],
                           "prompt", self.repo, self.artifacts, 30)
        self.assertLess(time.monotonic() - started, 5)

    def test_unique_artifacts_and_failed_round_does_not_reuse_reply(self):
        _, _, first, _ = self.invoke()
        code, record, second, _ = self.invoke(case="empty")
        self.assertNotEqual(first, second)
        self.assertEqual(code, 1)
        self.assertNotIn("response", record)

    def test_snapshot_covers_staged_unstaged_deleted_and_new_files(self):
        (self.repo / "existing.py").write_text("staged version\n")
        self.git("add", "existing.py")
        (self.repo / "existing.py").write_text("unstaged final version\n")
        (self.repo / "delete.py").unlink()
        (self.repo / "new.py").write_text("brand new\n")
        snap = runner.snapshot(self.repo, self.base)
        self.assertEqual({f["path"] for f in snap["files"]}, {"existing.py", "delete.py", "new.py"})
        self.assertEqual(next(f for f in snap["files"] if f["path"] == "delete.py")["kind"], "deleted")
        self.assertEqual(next(f for f in snap["files"] if f["path"] == "existing.py")["sha256"],
                         runner.digest((self.repo / "existing.py").read_bytes()))

    def test_inspection_requires_other_provider_and_fresh_session(self):
        code, _, _, error = self.invoke(mode="inspect", extra=("--base", self.base, "--provider", "claude"))
        self.assertEqual(code, 1)
        self.assertIn("opposite the builder", error)
        _, _, previous, _ = self.invoke()
        code, _, _, error = self.invoke(mode="inspect", extra=("--base", self.base, "--resume", str(previous)))
        self.assertEqual(code, 1)
        self.assertIn("fresh session", error)

    def test_changed_code_during_inspection_fails(self):
        (self.repo / "new.py").write_text("original new file")
        code, record, _, _ = self.invoke(mode="inspect", case="mutate_code", extra=("--base", self.base))
        self.assertEqual(code, 1)
        self.assertIn("Code changed", record["error"])

    def test_build_requires_explicit_review_override_and_clean_tree(self):
        code, _, _, error = self.invoke(mode="build", extra=("--proof", "python -m unittest"))
        self.assertEqual(code, 1)
        self.assertIn("--approval", error)
        (self.repo / "user_work.py").write_text("preserve me")
        code, _, _, error = self.invoke(mode="build", extra=("--unreviewed-spec", "--proof", "test"))
        self.assertEqual(code, 1)
        self.assertIn("clean checkout", error)
        self.assertEqual((self.repo / "user_work.py").read_text(), "preserve me")

    def test_build_resume_keeps_initial_baseline_and_existing_build_changes(self):
        extra = ("--builder", "codex", "--unreviewed-spec", "--proof", "python -m unittest")
        code, record, path, _ = self.invoke(mode="build", case="build", extra=extra)
        self.assertEqual(code, 0, record)
        self.assertEqual(record["base"], self.base)
        code, record, _, _ = self.invoke(mode="build", case="build", extra=extra+("--resume", str(path)))
        self.assertEqual(code, 0, record)
        self.assertEqual(record["base"], self.base)
        (self.repo / "user_work.py").write_text("intervening edit")
        code, _, _, error = self.invoke(mode="build", case="build", extra=extra+("--resume", str(path)))
        self.assertEqual(code, 1)
        self.assertIn("Checkout changed", error)

    def test_artifacts_cannot_contaminate_target_checkout(self):
        code, _, _, error = self.invoke(extra=("--artifacts", str(self.repo / "runs")))
        self.assertEqual(code, 1)
        self.assertIn("outside", error)


if __name__ == "__main__":
    unittest.main()
