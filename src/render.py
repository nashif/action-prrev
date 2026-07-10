# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Markdown rendering for the summary comment and inline review comments."""

from __future__ import annotations

from typing import Any

from github_api import file_url
from review import Finding, Review

SEVERITY_BADGE = {
    "critical": "🔴 critical",
    "high": "🟠 high",
    "medium": "🟡 medium",
    "low": "🔵 low",
}

VERDICT_BADGE = {
    "approve": "✅ **Approve** — no blocking issues found.",
    "comment": "💬 **Comment** — worth a look, nothing blocking.",
    "request_changes": "🛑 **Request changes** — at least one issue should be fixed before merge.",
}

SECTIONS = [
    ("bugs", "🐞 Potential bugs"),
    ("security", "🔐 Security & vulnerabilities"),
    ("performance", "⚡ Performance"),
    ("suggestions", "💡 Suggestions"),
    ("best_practices", "📐 Best practices"),
]

EXT_TO_LANG = {
    "py": "python", "js": "javascript", "jsx": "jsx", "ts": "typescript", "tsx": "tsx",
    "go": "go", "rs": "rust", "java": "java", "kt": "kotlin", "rb": "ruby", "php": "php",
    "c": "c", "h": "c", "cc": "cpp", "cpp": "cpp", "hpp": "cpp", "cs": "csharp",
    "sh": "bash", "bash": "bash", "zsh": "bash", "yml": "yaml", "yaml": "yaml",
    "json": "json", "toml": "toml", "sql": "sql", "swift": "swift", "scala": "scala",
    "tf": "hcl", "dockerfile": "dockerfile", "md": "markdown",
}


def marker(tag: str) -> str:
    return f"<!-- {tag} -->"


def _lang_for(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else path.lower()
    return EXT_TO_LANG.get(ext, "")


def _score_bar(score: int) -> str:
    filled = round(score / 10)
    return "█" * filled + "░" * (10 - filled)


def _location(finding: Finding, repo: str, sha: str) -> str:
    if not finding.file:
        return "_general_"
    label = f"`{finding.file}`" if not finding.line else f"`{finding.file}:{finding.line}`"
    url = file_url(repo, sha, finding.file, finding.line or None)
    return f"[{label}]({url})"


def _render_finding(finding: Finding, index: int, repo: str, sha: str) -> str:
    badge = SEVERITY_BADGE.get(finding.severity, finding.severity)
    cwe = f" · [{finding.cwe}](https://cwe.mitre.org/data/definitions/{finding.cwe.split('-')[-1]}.html)" if finding.cwe.startswith("CWE-") else ""
    lines = [f"#### {index}. {finding.title}", "", f"{badge} · {_location(finding, repo, sha)}{cwe}", ""]
    if finding.description:
        lines += [finding.description, ""]
    if finding.recommendation:
        lines += [f"**Recommendation:** {finding.recommendation}", ""]
    if finding.suggested_code.strip():
        lang = _lang_for(finding.file)
        lines += [f"```{lang}", finding.suggested_code.rstrip(), "```", ""]
    return "\n".join(lines)


def _sections_worst_first(review: Review) -> list[tuple[str, str]]:
    """Order sections by the severity of their worst finding, ties broken by SECTIONS order."""

    def worst(item: tuple[int, tuple[str, str]]) -> tuple[int, int]:
        index, (category, _) = item
        findings = review.by_category(category)
        return (-max((f.rank for f in findings), default=-1), index)

    return [section for _, section in sorted(enumerate(SECTIONS), key=worst)]


def _render_section(review: Review, category: str, heading: str, repo: str, sha: str) -> str:
    findings = review.by_category(category)
    if not findings:
        return ""
    worst = SEVERITY_BADGE.get(findings[0].severity, "")
    body = "\n".join(_render_finding(f, i, repo, sha) for i, f in enumerate(findings, start=1))
    open_attr = " open" if category in ("bugs", "security") else ""
    return (
        f"<details{open_attr}>\n"
        f"<summary><b>{heading}</b> — {len(findings)} finding(s), highest {worst}</summary>\n\n"
        f"{body}\n</details>\n"
    )


def comment_body(review: Review, cfg, pr, repo: str) -> str:
    sha = pr.head_sha
    counts = {category: len(review.by_category(category)) for category, _ in SECTIONS}
    total = sum(counts.values())

    parts = [
        marker(cfg.comment_tag),
        "## 🤖 AI Pull Request Review",
        "",
        VERDICT_BADGE.get(review.verdict, review.verdict),
        "",
        f"**Score:** `{_score_bar(review.score)}` **{review.score}/100** · "
        f"**Findings:** {total} · **Confidence:** {review.confidence}",
        "",
    ]

    if review.summary:
        parts += ["### Summary", "", review.summary, ""]

    if total:
        tally = " · ".join(f"{heading.split(' ', 1)[1]}: {counts[key]}" for key, heading in SECTIONS if counts[key])
        parts += [f"> {tally}", ""]
        for category, heading in _sections_worst_first(review):
            section = _render_section(review, category, heading, repo, sha)
            if section:
                parts.append(section)
    else:
        parts += ["### Findings", "", "Nothing to flag. The diff reads clean against every category checked.", ""]

    if review.final_comments:
        parts += ["### 📋 Final comments", "", review.final_comments, ""]

    parts += ["---", "", _footer(review, pr)]
    return "\n".join(parts)


def _footer(review: Review, pr) -> str:
    bits = [f"Model: `{', '.join(review.models_used) or 'unknown'}`", f"Commit: `{pr.head_sha[:7]}`"]
    if review.chunks > 1:
        bits.append(f"Diff reviewed in {review.chunks} slices")
    if review.truncated:
        bits.append("⚠️ diff truncated — some files were not reviewed")
    if review.excluded_files:
        bits.append(f"{len(review.excluded_files)} file(s) excluded")
    tokens = review.prompt_tokens + review.completion_tokens
    if tokens:
        cost = f", ${review.cost:.4f}" if review.cost else ""
        bits.append(f"{tokens:,} tokens{cost}")
    return (
        "<sub>"
        + " · ".join(bits)
        + "<br/>Generated by an AI model. Treat findings as a second opinion, not a gate — verify before acting.</sub>"
    )


def inline_comments(review: Review, commentable: dict[str, set[int]]) -> list[dict[str, Any]]:
    """Build review comments for findings that land on a line GitHub will accept."""
    comments: list[dict[str, Any]] = []
    for finding in review.findings:
        if finding.line <= 0 or finding.line not in commentable.get(finding.file, set()):
            continue
        badge = SEVERITY_BADGE.get(finding.severity, finding.severity)
        body = [f"**{badge} · {finding.title}**", "", finding.description]
        if finding.recommendation:
            body += ["", f"**Recommendation:** {finding.recommendation}"]
        if finding.suggested_code.strip():
            # GitHub renders a `suggestion` block as a one-click commit.
            body += ["", "```suggestion", finding.suggested_code.rstrip(), "```"]
        comments.append({"path": finding.file, "line": finding.line, "side": "RIGHT", "body": "\n".join(body)})
    return comments


def step_summary(review: Review) -> str:
    rows = [f"| {heading} | {len(review.by_category(key))} |" for key, heading in SECTIONS]
    return "\n".join(
        [
            "## AI PR Review",
            "",
            f"**Verdict:** {review.verdict} · **Score:** {review.score}/100 · "
            f"**Highest severity:** {review.highest_severity}",
            "",
            "| Category | Findings |",
            "| --- | ---: |",
            *rows,
            "",
            review.summary,
        ]
    )
