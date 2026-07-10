# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Runs the model over the diff and normalizes whatever comes back."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import prompts
from config import SEVERITY_ORDER, Config
from diffparse import Chunk
from openrouter import OpenRouterClient, parse_json

log = logging.getLogger(__name__)

VALID_SEVERITIES = set(prompts.SEVERITIES)
VALID_VERDICTS = {"approve", "comment", "request_changes"}


@dataclass
class Finding:
    category: str
    file: str
    line: int
    severity: str
    title: str
    description: str
    recommendation: str
    suggested_code: str = ""
    cwe: str = ""

    @property
    def rank(self) -> int:
        return SEVERITY_ORDER.index(self.severity)

    def key(self) -> tuple[str, str, int, str]:
        return (self.category, self.file, self.line, self.title.strip().lower())


@dataclass
class Review:
    summary: str = ""
    score: int = 0
    verdict: str = "comment"
    confidence: str = "medium"
    final_comments: str = ""
    test_assessment: str = ""
    findings: list[Finding] = field(default_factory=list)
    models_used: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    chunks: int = 1
    truncated: bool = False
    excluded_files: list[str] = field(default_factory=list)
    context_chars: int = 0

    def by_category(self, category: str) -> list[Finding]:
        return [f for f in self.findings if f.category == category]

    @property
    def highest_severity(self) -> str:
        if not self.findings:
            return "none"
        return max(self.findings, key=lambda f: f.rank).severity


def _clean(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _findings_from(payload: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    for category in prompts.CATEGORIES:
        for raw in payload.get(category) or []:
            if not isinstance(raw, dict):
                continue
            title = _clean(raw.get("title"))
            description = _clean(raw.get("description"))
            if not title and not description:
                continue

            severity = _normalize_severity(_clean(raw.get("severity")))

            try:
                line = int(raw.get("line") or 0)
            except (TypeError, ValueError):
                line = 0

            findings.append(
                Finding(
                    category=category,
                    file=_clean(raw.get("file")),
                    line=max(line, 0),
                    severity=severity,
                    title=title or description[:80],
                    description=description,
                    recommendation=_clean(raw.get("recommendation")),
                    suggested_code=raw.get("suggested_code") if isinstance(raw.get("suggested_code"), str) else "",
                    cwe=_clean(raw.get("cwe")),
                )
            )
    return findings


# Profiles are free to name severities their own community uses; map them onto the
# four the schema, the gate, and the renderer all agree on.
SEVERITY_ALIASES = {
    "suggestion": "low",
    "suggestions": "low",
    "info": "low",
    "informational": "low",
    "nit": "low",
    "minor": "low",
    "major": "high",
    "blocker": "critical",
    "severe": "critical",
}


def _normalize_severity(value: str) -> str:
    value = value.lower()
    if value in VALID_SEVERITIES:
        return value
    return SEVERITY_ALIASES.get(value, "medium")


def _normalize_verdict(value: str) -> str:
    """`request changes`, `request-changes`, `REQUEST_CHANGES` all mean the same thing."""
    return value.strip().lower().replace(" ", "_").replace("-", "_")


def _dedupe(findings: list[Finding]) -> list[Finding]:
    seen: set[tuple[str, str, int, str]] = set()
    unique: list[Finding] = []
    for finding in findings:
        key = finding.key()
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    return unique


def _sort(findings: list[Finding]) -> list[Finding]:
    order = {category: index for index, category in enumerate(prompts.CATEGORIES)}
    return sorted(findings, key=lambda f: (-f.rank, order.get(f.category, 99), f.file, f.line))


def _clamp_score(value: Any) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return 50


def _digest(findings: list[Finding], limit: int = 60) -> str:
    if not findings:
        return "No findings were reported in any pass."
    lines = []
    for finding in _sort(findings)[:limit]:
        location = f"{finding.file}:{finding.line}" if finding.file else "general"
        lines.append(f"- [{finding.severity}] ({finding.category}) {location} — {finding.title}")
    if len(findings) > limit:
        lines.append(f"- ...and {len(findings) - limit} more lower-severity findings.")
    return "\n".join(lines)


def run(
    client: OpenRouterClient,
    cfg: Config,
    pr,
    chunks: list[Chunk],
    files_summary: str,
    *,
    profile: str,
    repo: str = "",
    repo_overview: str = "",
    context_for: Callable[[list[str]], str] | None = None,
) -> Review:
    """Review each diff chunk, then synthesize one verdict when there was more than one."""
    system = prompts.system_prompt(cfg.language, profile)
    review = Review(chunks=len(chunks))
    partials: list[dict[str, Any]] = []

    for index, slice_ in enumerate(chunks, start=1):
        chunk_info = (
            f"This is slice {index} of {len(chunks)} of a large pull request. "
            "Judge only what you can see here; another pass covers the rest."
            if len(chunks) > 1
            else ""
        )
        # Context is scoped to the files in this slice, so a 12-file PR does not
        # pay for 12 files of source on every one of its slices.
        file_context = context_for(slice_.paths) if context_for else ""
        if file_context:
            review.context_chars += len(file_context)

        diff = slice_.diff
        user = prompts.review_prompt(
            pr,
            diff,
            files_summary,
            cfg.project_context,
            repo=repo,
            repo_overview=repo_overview,
            file_context=file_context,
            chunk_info=chunk_info,
        )

        log.info(
            "Reviewing slice %d/%d (%d diff chars, %d context chars)",
            index,
            len(chunks),
            len(diff),
            len(file_context),
        )
        completion = client.complete(
            models=cfg.models,
            system=system,
            user=user,
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            json_schema=prompts.REVIEW_SCHEMA,
        )
        _account(review, completion)

        try:
            payload = parse_json(completion.content)
        except ValueError as exc:
            log.error("Slice %d returned unusable output: %s", index, exc)
            continue

        partials.append(payload)
        review.findings.extend(_findings_from(payload))

    if not partials:
        raise RuntimeError("No slice of the diff produced a usable review.")

    review.findings = _sort(_dedupe(review.findings))

    if len(partials) == 1:
        payload = partials[0]
        review.summary = _clean(payload.get("summary"))
        review.score = _clamp_score(payload.get("score"))
        review.verdict = _normalize_verdict(_clean(payload.get("verdict")))
        review.confidence = _clean(payload.get("confidence")).lower() or "medium"
        review.final_comments = _clean(payload.get("final_comments"))
        review.test_assessment = _clean(payload.get("test_assessment"))
    else:
        _synthesize(client, cfg, pr, review, profile)

    if review.verdict not in VALID_VERDICTS:
        review.verdict = "request_changes" if review.highest_severity in ("high", "critical") else "comment"

    return review


def _synthesize(client: OpenRouterClient, cfg: Config, pr, review: Review, profile: str) -> None:
    user = prompts.synthesis_prompt(pr, _digest(review.findings), review.chunks, cfg.language)
    try:
        completion = client.complete(
            models=cfg.models,
            system=prompts.system_prompt(cfg.language, profile),
            user=user,
            temperature=cfg.temperature,
            max_tokens=2000,
            json_schema=prompts.SYNTHESIS_SCHEMA,
        )
        _account(completion=completion, review=review)
        payload = parse_json(completion.content)
    except (RuntimeError, ValueError) as exc:
        log.warning("Synthesis pass failed (%s); deriving the verdict from the findings", exc)
        review.summary = f"Reviewed {review.chunks} slices of a large pull request."
        review.score = _score_from_findings(review)
        review.verdict = "request_changes" if review.highest_severity in ("high", "critical") else "comment"
        review.confidence = "low"
        review.final_comments = "The per-slice findings below stand on their own; no overall synthesis was produced."
        return

    review.summary = _clean(payload.get("summary"))
    review.score = _clamp_score(payload.get("score"))
    review.verdict = _normalize_verdict(_clean(payload.get("verdict")))
    review.confidence = _clean(payload.get("confidence")).lower() or "medium"
    review.final_comments = _clean(payload.get("final_comments"))
    review.test_assessment = _clean(payload.get("test_assessment"))


def _score_from_findings(review: Review) -> int:
    penalty = {"critical": 30, "high": 15, "medium": 5, "low": 1}
    return max(0, 100 - sum(penalty.get(f.severity, 0) for f in review.findings))


def _account(review: Review, completion) -> None:
    review.prompt_tokens += completion.prompt_tokens
    review.completion_tokens += completion.completion_tokens
    if completion.cost:
        review.cost += completion.cost
    if completion.model not in review.models_used:
        review.models_used.append(completion.model)
