# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""System/user prompts and the JSON schema the model must fill in."""

from __future__ import annotations

from typing import Any

SEVERITIES = ["low", "medium", "high", "critical"]
CATEGORIES = ["bugs", "security", "suggestions", "performance", "best_practices"]


def _finding_schema(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "file": {"type": "string", "description": "Repository-relative path, exactly as it appears in the diff."},
        "line": {"type": "integer", "description": "Line number in the new version of the file. Use 0 if not line-specific."},
        "severity": {"type": "string", "enum": SEVERITIES},
        "title": {"type": "string", "description": "One short sentence naming the problem."},
        "description": {"type": "string", "description": "What is wrong and the concrete conditions under which it goes wrong."},
        "recommendation": {"type": "string", "description": "The specific change to make."},
        "suggested_code": {
            "type": "string",
            "description": "Replacement code for the cited lines, or an empty string when a snippet would not help.",
        },
    }
    properties.update(extra or {})
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "score", "verdict", "confidence", *CATEGORIES, "test_assessment", "final_comments"],
    "properties": {
        "summary": {"type": "string", "description": "Two or three sentences on what this pull request does."},
        "score": {"type": "integer", "description": "Overall quality of the change, 0 (unmergeable) to 100 (exemplary)."},
        "verdict": {"type": "string", "enum": ["approve", "comment", "request_changes"]},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "bugs": {
            "type": "array",
            "description": "Correctness defects: logic errors, unhandled cases, race conditions, resource leaks, broken contracts.",
            "items": _finding_schema(),
        },
        "security": {
            "type": "array",
            "description": "Exploitable weaknesses: injection, authn/authz gaps, secrets, unsafe deserialization, path traversal, SSRF, weak crypto.",
            "items": _finding_schema(
                {"cwe": {"type": "string", "description": "CWE identifier such as CWE-89, or an empty string."}}
            ),
        },
        "suggestions": {
            "type": "array",
            "description": "Improvements to clarity, structure, reuse, or testability.",
            "items": _finding_schema(),
        },
        "performance": {
            "type": "array",
            "description": "Algorithmic complexity, redundant work, N+1 queries, allocations on hot paths, blocking I/O.",
            "items": _finding_schema(),
        },
        "best_practices": {
            "type": "array",
            "description": "Deviations from language, framework, or repository conventions.",
            "items": _finding_schema(),
        },
        "test_assessment": {
            "type": "string",
            "description": (
                "What behavior the change tests, what material behavior it leaves untested, and whether tests "
                "are required before merge. Empty string when the change needs no tests."
            ),
        },
        "final_comments": {"type": "string", "description": "Closing guidance for the author: what to fix first, what can wait."},
    },
}

SYNTHESIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "score", "verdict", "confidence", "test_assessment", "final_comments"],
    "properties": {
        key: REVIEW_SCHEMA["properties"][key]
        for key in ("summary", "score", "verdict", "confidence", "test_assessment", "final_comments")
    },
}


# Appended to every profile. This, not the profile, governs the shape of the answer:
# a profile is free to describe a Markdown review, and the contract still holds.
OUTPUT_CONTRACT = """\

---

# Output contract

The instructions above describe *what* to review. This section governs *how* to answer, and \
overrides any formatting, section layout, or heading structure requested above.

Return exactly one JSON object conforming to the supplied schema. Emit no prose, no Markdown, \
and no code fences outside it.

Map your review onto the schema's fields:

- `summary`: what the pull request does and which areas it affects.
- `bugs`, `security`, `performance`, `suggestions`, `best_practices`: arrays of findings. Put each \
finding in the one array that fits it best; do not repeat a finding across arrays.
- `test_assessment`: what behavior the change tests, what material behavior it leaves untested, and \
whether tests are required before merge. Empty string when the change needs no tests.
- `final_comments`: closing guidance for the author -- what to fix first, what can wait.
- `score` (0-100), `verdict`, `confidence`.

Every finding object must set:

- `file`: a path exactly as it appears in the diff.
- `line`: a line number on the new side of the diff (the numbering established by the @@ hunk \
headers). Use 0 when the concern is not anchored to one changed line -- architecture, missing tests, \
cross-file behavior, or API design. A finding with line 0 appears in the summary comment only, never \
as an inline comment.
- `severity`: exactly one of `low`, `medium`, `high`, `critical`. Where the guidance above offers a \
`suggestion` severity, express it as severity `low` in the `suggestions` array.
- `title`: one short sentence naming the problem.
- `description`: the problem, the conditions that trigger it, and its impact.
- `recommendation`: the specific change to make.
- `suggested_code`: a drop-in replacement for the cited lines, correctly indented, with no diff \
markers (no leading + or -). Empty string when a snippet would not clarify the fix.
- `cwe` (security findings only): a CWE identifier such as `CWE-89`, or an empty string.

`verdict` must be exactly `approve`, `comment`, or `request_changes` -- with an underscore, never a space.

Empty arrays are the correct answer for a clean diff. Write all prose in {language}.\
"""


def _pr_header(pr, files_summary: str, project_context: str, repo_overview: str, repo: str) -> str:
    parts = [
        f"## Pull request #{pr.number}: {pr.title}",
        f"Repository: `{repo}`",
        f"Base branch: `{pr.base_ref}` <- head: `{pr.head_ref}`",
        f"Changed files: {pr.changed_files} (+{pr.additions} / -{pr.deletions})",
    ]
    if pr.body.strip():
        parts.append(f"\n### Author's description\n{pr.body.strip()[:4000]}")
    if repo_overview:
        parts.append(f"\n### About this repository\n{repo_overview}")
    if project_context:
        parts.append(f"\n### Maintainer's review guidance\n{project_context[:6000]}")
    if files_summary:
        parts.append(f"\n### Files under review\n{files_summary}")
    return "\n".join(parts)


def review_prompt(
    pr,
    diff: str,
    files_summary: str,
    project_context: str,
    *,
    repo: str = "",
    repo_overview: str = "",
    file_context: str = "",
    chunk_info: str = "",
) -> str:
    header = _pr_header(pr, files_summary, project_context, repo_overview, repo)
    scope = f"\n\n> {chunk_info}" if chunk_info else ""

    body = [f"{header}{scope}", ""]
    if file_context:
        body += [
            "### Surrounding code at the head commit",
            "",
            "These are the current contents of each changed file around the lines the diff touches, "
            "with real line numbers. The diff below is already applied to them. Read these before judging "
            "whether a change is correct — they show the callers, guards, and helpers the diff cannot.",
            "",
            file_context,
            "",
        ]
    body += [
        "### Diff",
        "",
        "```diff",
        diff,
        "```",
        "",
        "Review the changed lines and return the JSON object.",
    ]
    return "\n".join(body)


def synthesis_prompt(pr, findings_digest: str, chunk_count: int, language: str) -> str:
    return (
        f"A large pull request (#{pr.number}: {pr.title}) was reviewed in {chunk_count} separate passes, "
        "one per slice of the diff. Below are all findings from every pass.\n\n"
        f"{findings_digest}\n\n"
        "Write the overall verdict for the pull request as a whole: a `summary` of what it does, a `score` from 0 to 100, "
        "a `verdict`, your `confidence`, and `final_comments` telling the author what to fix first and what can wait. "
        "Weigh the findings by severity rather than counting them. "
        f"Write the prose in {language}. Return only the JSON object."
    )


def system_prompt(language: str, profile: str) -> str:
    """Domain guidance from the profile, then the output contract that overrides its formatting."""
    return profile.rstrip() + "\n" + OUTPUT_CONTRACT.format(language=language)
