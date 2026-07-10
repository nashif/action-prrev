# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""End-to-end run of main.py against stub GitHub and OpenRouter servers."""

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.test_review import PAYLOAD

ROOT = Path(__file__).resolve().parent.parent

DIFF = """diff --git a/app/auth.py b/app/auth.py
--- a/app/auth.py
+++ b/app/auth.py
@@ -10,6 +10,9 @@ def login(request):
     user = lookup(request.form["email"])
     if not user:
         return None
+    query = "SELECT * FROM sessions WHERE token = '" + token + "'"
+    db.execute(query)
+    return user
"""

AUTH_SOURCE = """import db


def lookup(email):
    return db.users.get(email)


def login(request):
    token = request.headers["X-Token"]
    user = lookup(request.form["email"])
    if not user:
        return None
    query = "SELECT * FROM sessions WHERE token = '" + token + "'"
    db.execute(query)
    return user


def logout():
    pass
"""

REPO_JSON = {"description": "A payments service.", "language": "Python", "topics": ["fintech"]}

TREE_JSON = {
    "tree": [
        {"path": "README.md", "type": "blob"},
        {"path": "pyproject.toml", "type": "blob"},
        {"path": "app/auth.py", "type": "blob"},
        {"path": "app/routes.py", "type": "blob"},
    ]
}

PR_JSON = {
    "number": 1,
    "title": "Add session lookup",
    "body": "Closes #3",
    "base": {"ref": "main"},
    "head": {"ref": "feature", "sha": "abcdef1234567890"},
    "draft": False,
    "labels": [],
    "changed_files": 1,
    "additions": 3,
    "deletions": 0,
}


class Handler(BaseHTTPRequestHandler):
    requests: list[tuple[str, str, dict]] = []

    def log_message(self, *args):
        pass

    def _send(self, status, body, content_type="application/json"):
        payload = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        Handler.requests.append(("GET", self.path, {}))
        if self.path.startswith("/repos/acme/app/pulls/1") and "files" not in self.path:
            if "diff" in self.headers.get("Accept", ""):
                return self._send(200, DIFF, "text/plain")
            return self._send(200, PR_JSON)
        if self.path.startswith("/repos/acme/app/issues/1/comments"):
            return self._send(200, [])  # no previous comment to update
        if self.path.startswith("/repos/acme/app/contents/app/auth.py"):
            return self._send(200, AUTH_SOURCE, "text/plain")
        if self.path.startswith("/repos/acme/app/git/trees/"):
            return self._send(200, TREE_JSON)
        if self.path == "/repos/acme/app/readme":
            return self._send(200, "# Payments\n\nHandles money.", "text/plain")
        if self.path == "/repos/acme/app":
            return self._send(200, REPO_JSON)
        return self._send(404, {"message": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        Handler.requests.append(("POST", self.path, body))

        if self.path.endswith("/chat/completions"):
            return self._send(
                200,
                {
                    "model": body["model"],
                    "choices": [{"message": {"content": json.dumps(PAYLOAD)}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1200, "completion_tokens": 300, "cost": 0.0042},
                },
            )
        if self.path == "/repos/acme/app/issues/1/comments":
            return self._send(201, {"html_url": "https://github.com/acme/app/pull/1#issuecomment-9"})
        if self.path == "/repos/acme/app/pulls/1/reviews":
            return self._send(200, {"id": 5})
        return self._send(404, {"message": "not found"})


@pytest.fixture
def server():
    Handler.requests = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def run_action(server_url, tmp_path, **inputs):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 1}}))
    outputs = tmp_path / "outputs.txt"
    outputs.touch()
    summary = tmp_path / "summary.md"
    summary.touch()

    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(ROOT / "src"),
        "GITHUB_REPOSITORY": "acme/app",
        "GITHUB_API_URL": server_url,
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_OUTPUT": str(outputs),
        "GITHUB_STEP_SUMMARY": str(summary),
        "INPUT_OPENROUTER_API_KEY": "sk-test",
        "INPUT_GITHUB_TOKEN": "ghs-test",
        "INPUT_BASE_URL": f"{server_url}/api/v1",
        "INPUT_MODEL": "test/model",
    }
    env.update(inputs)

    proc = subprocess.run(
        [sys.executable, str(ROOT / "src" / "main.py")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    parsed = _parse_outputs(outputs.read_text())
    return proc, parsed, summary.read_text()


def _parse_outputs(raw: str) -> dict[str, str]:
    result, lines = {}, raw.splitlines()
    index = 0
    while index < len(lines):
        name, _, delimiter = lines[index].partition("<<")
        index += 1
        value = []
        while index < len(lines) and lines[index] != delimiter:
            value.append(lines[index])
            index += 1
        index += 1
        result[name] = "\n".join(value)
    return result


def test_happy_path_posts_a_comment_and_sets_outputs(server, tmp_path):
    proc, outputs, summary = run_action(server, tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert outputs["skipped"] == "false"
    assert outputs["score"] == "42"
    assert outputs["verdict"] == "request_changes"
    assert outputs["findings_count"] == "3"
    assert outputs["highest_severity"] == "critical"
    assert outputs["comment_url"].endswith("#issuecomment-9")
    assert "Highest severity:** critical" in summary

    posted = [body for method, path, body in Handler.requests if path.endswith("/issues/1/comments") and method == "POST"]
    assert len(posted) == 1
    assert "<!-- ai-pr-review -->" in posted[0]["body"]
    assert "SQL injection" in posted[0]["body"]

    completion = next(body for _, path, body in Handler.requests if path.endswith("/chat/completions"))
    assert completion["model"] == "test/model"
    assert completion["response_format"]["json_schema"]["strict"] is True
    assert "SELECT * FROM sessions" in completion["messages"][1]["content"]


def test_fail_on_severity_fails_the_step_but_still_comments(server, tmp_path):
    proc, outputs, _ = run_action(server, tmp_path, INPUT_FAIL_ON_SEVERITY="high")

    assert proc.returncode == 1
    assert "::error title=AI PR Review::" in proc.stdout
    assert outputs["comment_url"], "the comment must still be posted when the gate fails"


def test_min_score_gate(server, tmp_path):
    proc, _, _ = run_action(server, tmp_path, INPUT_MIN_SCORE="70")
    assert proc.returncode == 1
    assert "below the required minimum" in proc.stdout


def test_inline_comments_are_anchored_to_added_lines(server, tmp_path):
    proc, _, _ = run_action(server, tmp_path, INPUT_POST_INLINE_COMMENTS="true")
    assert proc.returncode == 0

    review = next(body for _, path, body in Handler.requests if path.endswith("/pulls/1/reviews"))
    assert review["commit_id"] == "abcdef1234567890"
    # Only line 13 and 15 exist on the new side; the line-0 finding is dropped.
    assert sorted(c["line"] for c in review["comments"]) == [13, 15]
    assert "```suggestion" in review["comments"][0]["body"]


def test_skip_label_short_circuits_before_calling_the_model(server, tmp_path):
    Handler.requests = []
    proc, outputs, summary = run_action(server, tmp_path, INPUT_SKIP_LABELS="add-me")

    # The stub PR has no labels, so this should NOT skip; sanity-check the inverse first.
    assert outputs["skipped"] == "false"

    # Now teach the stub to return the label and re-run.
    PR_JSON["labels"] = [{"name": "Add-Me"}]
    try:
        Handler.requests = []
        proc, outputs, summary = run_action(server, tmp_path, INPUT_SKIP_LABELS="add-me")
        assert proc.returncode == 0
        assert outputs["skipped"] == "true"
        assert outputs["verdict"] == "skipped"
        assert "Skipped" in summary
        assert not any(path.endswith("/chat/completions") for _, path, _ in Handler.requests)
    finally:
        PR_JSON["labels"] = []


def test_draft_pull_requests_are_skipped(server, tmp_path):
    PR_JSON["draft"] = True
    try:
        proc, outputs, _ = run_action(server, tmp_path)
        assert proc.returncode == 0
        assert outputs["skipped"] == "true"
        assert not any(path.endswith("/chat/completions") for _, path, _ in Handler.requests)
    finally:
        PR_JSON["draft"] = False


def test_required_label_absent_skips(server, tmp_path):
    proc, outputs, _ = run_action(server, tmp_path, INPUT_REQUIRED_LABELS="ai-review")
    assert proc.returncode == 0
    assert outputs["skipped"] == "true"


def _prompt(requests):
    completion = next(body for _, path, body in requests if path.endswith("/chat/completions"))
    return completion["messages"][1]["content"]


def _system(requests):
    completion = next(body for _, path, body in requests if path.endswith("/chat/completions"))
    return completion["messages"][0]["content"]


def test_prompt_carries_the_surrounding_source_of_changed_files(server, tmp_path):
    proc, _, _ = run_action(server, tmp_path, INPUT_CONTEXT_LINES="5")
    assert proc.returncode == 0

    prompt = _prompt(Handler.requests)
    assert "### Surrounding code at the head commit" in prompt
    assert "`app/auth.py` (19 lines)" in prompt
    # The real file is shown with real line numbers, including code the diff never
    # touched -- here, where `token` comes from, which the diff alone never reveals.
    assert "    8| def login(request):" in prompt
    assert '    9|     token = request.headers["X-Token"]' in prompt
    # ...and the diff still follows.
    assert "```diff" in prompt


def test_prompt_carries_the_repository_overview(server, tmp_path):
    proc, _, _ = run_action(server, tmp_path)
    assert proc.returncode == 0

    prompt = _prompt(Handler.requests)
    assert "### About this repository" in prompt
    assert "A payments service." in prompt
    assert "Primary language: Python" in prompt
    assert "`pyproject.toml`" in prompt
    assert "Handles money." in prompt


def test_context_can_be_disabled(server, tmp_path):
    proc, _, _ = run_action(
        server, tmp_path, INPUT_INCLUDE_FILE_CONTEXT="false", INPUT_INCLUDE_REPO_OVERVIEW="false"
    )
    assert proc.returncode == 0

    prompt = _prompt(Handler.requests)
    assert "Surrounding code" not in prompt
    assert "About this repository" not in prompt
    assert "```diff" in prompt
    # No context means no reason to touch the contents endpoint.
    assert not any("/contents/" in path for _, path, _ in Handler.requests)


def test_maintainer_guidance_reaches_the_prompt(server, tmp_path):
    proc, _, _ = run_action(server, tmp_path, INPUT_PROJECT_CONTEXT="Do not flag missing type hints.")
    assert proc.returncode == 0
    prompt = _prompt(Handler.requests)
    assert "### Maintainer's review guidance" in prompt
    assert "Do not flag missing type hints." in prompt


def test_default_profile_is_used_when_none_is_named(server, tmp_path):
    proc, _, _ = run_action(server, tmp_path)
    assert proc.returncode == 0
    system = _system(Handler.requests)
    assert "meticulous senior software engineer" in system
    assert "# Output contract" in system


def test_zephyr_profile_reaches_the_model_as_the_system_message(server, tmp_path):
    proc, _, _ = run_action(server, tmp_path, INPUT_REVIEW_PROFILE="zephyr")
    assert proc.returncode == 0

    system = _system(Handler.requests)
    assert system.startswith("You are an automated pull-request reviewer for the Zephyr Project.")
    assert "Devicetree" in system and "Kconfig" in system
    # The contract is appended after the profile and still demands JSON.
    assert system.index("Review discipline") < system.index("# Output contract")
    assert "Return exactly one JSON object" in system
    # ...and the JSON schema is still enforced at the API layer.
    completion = next(body for _, path, body in Handler.requests if path.endswith("/chat/completions"))
    assert completion["response_format"]["json_schema"]["strict"] is True


def test_a_custom_profile_file_is_read_from_the_workspace(server, tmp_path):
    (tmp_path / "ci").mkdir()
    (tmp_path / "ci" / "house.md").write_text("Review only for thread safety.")
    proc, _, _ = run_action(
        server, tmp_path, INPUT_REVIEW_PROFILE="ci/house.md", GITHUB_WORKSPACE=str(tmp_path)
    )
    assert proc.returncode == 0
    assert _system(Handler.requests).startswith("Review only for thread safety.")


def test_an_unknown_profile_fails_the_step_before_spending_a_token(server, tmp_path):
    proc, _, _ = run_action(server, tmp_path, INPUT_REVIEW_PROFILE="zepyhr")
    assert proc.returncode == 1
    assert "unknown review_profile" in proc.stdout + proc.stderr
    # Fails before any network call at all, not just before the model call.
    assert Handler.requests == []
