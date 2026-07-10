# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

import pytest

import openrouter
import render
import review as review_mod
from config import Config
from github_api import PullRequest

PAYLOAD = {
    "summary": "Adds a session lookup to the login path.",
    "score": 42,
    "verdict": "request_changes",
    "confidence": "high",
    "bugs": [
        {
            "file": "app/auth.py",
            "line": 15,
            "severity": "medium",
            "title": "login() returns before logging the attempt",
            "description": "The early return skips the audit log.",
            "recommendation": "Log before returning.",
            "suggested_code": "    audit(user)\n    return user",
        }
    ],
    "security": [
        {
            "file": "app/auth.py",
            "line": 13,
            "severity": "critical",
            "cwe": "CWE-89",
            "title": "SQL injection via string-concatenated token",
            "description": "`token` is interpolated straight into the query.",
            "recommendation": "Use a parameterized query.",
            "suggested_code": 'db.execute("SELECT * FROM sessions WHERE token = ?", (token,))',
        }
    ],
    "suggestions": [],
    "performance": [],
    "best_practices": [
        {
            "file": "app/auth.py",
            "line": 0,
            "severity": "low",
            "title": "Missing type hints",
            "description": "",
            "recommendation": "Annotate login().",
            "suggested_code": "",
        }
    ],
    "final_comments": "Fix the injection first.",
}


def make_config(**overrides):
    base = dict(
        api_key="k",
        github_token="t",
        model="test/model",
        fallback_models=[],
        base_url="https://example.invalid/v1",
        temperature=0.1,
        max_tokens=100,
    )
    base.update(overrides)
    return Config(**base)


def make_pr():
    return PullRequest(
        number=7,
        title="Add session lookup",
        body="",
        base_ref="main",
        head_ref="feature",
        head_sha="abcdef1234567890",
        draft=False,
        labels=[],
        changed_files=1,
        additions=3,
        deletions=0,
    )


def make_review():
    result = review_mod.Review(
        summary=PAYLOAD["summary"],
        score=PAYLOAD["score"],
        verdict=PAYLOAD["verdict"],
        confidence=PAYLOAD["confidence"],
        final_comments=PAYLOAD["final_comments"],
        findings=review_mod._sort(review_mod._findings_from(PAYLOAD)),
        models_used=["test/model"],
    )
    return result


# -- parsing ----------------------------------------------------------------


def test_findings_are_extracted_from_every_category():
    findings = review_mod._findings_from(PAYLOAD)
    assert {f.category for f in findings} == {"bugs", "security", "best_practices"}
    assert len(findings) == 3


def test_unknown_severity_falls_back_to_medium():
    payload = {"bugs": [{"title": "x", "description": "y", "severity": "spicy", "line": "nope"}]}
    (finding,) = review_mod._findings_from(payload)
    assert finding.severity == "medium"
    assert finding.line == 0


def test_findings_without_title_or_description_are_dropped():
    payload = {"bugs": [{"title": "", "description": "  ", "severity": "high"}]}
    assert review_mod._findings_from(payload) == []


def test_sort_puts_the_worst_first():
    findings = review_mod._sort(review_mod._findings_from(PAYLOAD))
    assert findings[0].severity == "critical"
    assert findings[-1].severity == "low"


def test_dedupe_collapses_repeats_across_slices():
    findings = review_mod._findings_from(PAYLOAD) * 3
    assert len(review_mod._dedupe(findings)) == 3


def test_highest_severity_and_score_clamping():
    assert make_review().highest_severity == "critical"
    assert review_mod._clamp_score(140) == 100
    assert review_mod._clamp_score(-3) == 0
    assert review_mod._clamp_score("not a number") == 50


def test_empty_review_reports_no_severity():
    assert review_mod.Review().highest_severity == "none"


# -- json recovery ----------------------------------------------------------


def test_parse_json_handles_bare_fenced_and_prefixed_output():
    assert openrouter.parse_json('{"a": 1}') == {"a": 1}
    assert openrouter.parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert openrouter.parse_json('Sure!\n```\n{"a": 1}\n```') == {"a": 1}
    assert openrouter.parse_json('Here you go: {"a": 1} — hope that helps') == {"a": 1}


def test_parse_json_raises_on_prose():
    with pytest.raises(ValueError, match="valid JSON"):
        openrouter.parse_json("I could not review this diff.")


# -- rendering --------------------------------------------------------------


def test_comment_body_contains_marker_score_and_sections():
    body = render.comment_body(make_review(), make_config(), make_pr(), "acme/app")
    assert body.startswith("<!-- ai-pr-review -->")
    assert "**42/100**" in body
    assert "Request changes" in body
    assert "🔐 Security & vulnerabilities" in body
    assert "🐞 Potential bugs" in body
    assert "CWE-89" in body
    assert "cwe.mitre.org/data/definitions/89.html" in body
    assert "Fix the injection first." in body
    # Security findings sort above medium bugs.
    assert body.index("SQL injection") < body.index("login() returns")


def test_comment_body_for_a_clean_diff():
    body = render.comment_body(review_mod.Review(score=95, verdict="approve"), make_config(), make_pr(), "acme/app")
    assert "reads clean" in body
    assert "Approve" in body


def test_truncation_and_exclusions_are_disclosed():
    result = make_review()
    result.truncated = True
    result.excluded_files = ["package-lock.json"]
    result.chunks = 3
    body = render.comment_body(result, make_config(), make_pr(), "acme/app")
    assert "diff truncated" in body
    assert "1 file(s) excluded" in body
    assert "3 slices" in body


def test_inline_comments_only_target_lines_present_in_the_diff():
    commentable = {"app/auth.py": {13, 14, 15}}
    comments = render.inline_comments(make_review(), commentable)
    # The line-0 best-practice finding is not anchorable and is left out.
    assert len(comments) == 2
    assert {c["line"] for c in comments} == {13, 15}
    assert all(c["side"] == "RIGHT" for c in comments)
    assert "```suggestion" in comments[0]["body"]


def test_inline_comments_skip_files_absent_from_the_diff():
    assert render.inline_comments(make_review(), {"other/file.py": {13}}) == []


def test_step_summary_is_valid_markdown_table():
    summary = render.step_summary(make_review())
    assert "| Category | Findings |" in summary
    assert "| 🔐 Security & vulnerabilities | 1 |" in summary
