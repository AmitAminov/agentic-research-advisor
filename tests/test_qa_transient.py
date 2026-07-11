"""qa_agent must retry transient claude API failures instead of reporting
an opaque "no parseable verdict" (root cause of the 2026-07-04 QA failure)."""
import qa_agent


def _write(p, text):
    p.write_text(text, encoding="utf-8")
    return p


class TestTransientFailureDetection:
    def test_529_overloaded_is_transient(self, tmp_path):
        log = _write(tmp_path / "qa.log",
                     "API Error: 529 Overloaded. This is a server-side issue, "
                     "usually temporary — try again in a moment.\n")
        assert qa_agent._transient_failure(log)

    def test_session_limit_is_transient(self, tmp_path):
        log = _write(tmp_path / "qa.log",
                     "You've hit your session limit · resets 10:20pm (Asia/Jerusalem)\n")
        assert qa_agent._transient_failure(log)

    def test_real_run_output_is_not_transient(self, tmp_path):
        # a genuine QA run produces plenty of output; even if the word
        # "overloaded" appears in it, it must not be classified as transient.
        log = _write(tmp_path / "qa.log",
                     "Inspecting index.html ...\n" * 200 + "the server was overloaded\n")
        assert qa_agent._transient_failure(log) is None

    def test_empty_or_missing_log_is_not_transient(self, tmp_path):
        assert qa_agent._transient_failure(tmp_path / "absent.log") is None
        assert qa_agent._transient_failure(_write(tmp_path / "e.log", "")) is None


class TestRunClaudeWithRetry:
    def test_retries_then_succeeds(self, tmp_path, monkeypatch):
        log = tmp_path / "qa.log"
        calls = []

        def fake_run(claude_exe, repo_root, cwd, prompt, minutes, logf):
            calls.append(1)
            if len(calls) == 1:
                _write(logf, "API Error: 529 Overloaded\n")
            else:
                _write(logf, "PASS 0 issues\n")
            return ("completed", 0)

        monkeypatch.setattr(qa_agent, "run_claude", fake_run)
        monkeypatch.setattr(qa_agent.time, "sleep", lambda s: None)
        status, code = qa_agent.run_claude_with_retry(
            "claude", tmp_path, tmp_path, "p", 1, log)
        assert status == "completed"
        assert len(calls) == 2

    def test_exhausted_retries_report_api_error(self, tmp_path, monkeypatch):
        log = tmp_path / "qa.log"

        def fake_run(claude_exe, repo_root, cwd, prompt, minutes, logf):
            _write(logf, "You've hit your session limit · resets 10:20pm\n")
            return ("completed", 1)

        monkeypatch.setattr(qa_agent, "run_claude", fake_run)
        monkeypatch.setattr(qa_agent.time, "sleep", lambda s: None)
        status, code = qa_agent.run_claude_with_retry(
            "claude", tmp_path, tmp_path, "p", 1, log)
        assert status == "api-error"

    def test_non_transient_completion_is_not_retried(self, tmp_path, monkeypatch):
        log = tmp_path / "qa.log"
        calls = []

        def fake_run(claude_exe, repo_root, cwd, prompt, minutes, logf):
            calls.append(1)
            _write(logf, "FAIL 3 issues\n")
            return ("completed", 0)

        monkeypatch.setattr(qa_agent, "run_claude", fake_run)
        status, code = qa_agent.run_claude_with_retry(
            "claude", tmp_path, tmp_path, "p", 1, log)
        assert status == "completed"
        assert len(calls) == 1
