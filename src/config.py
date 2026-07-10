"""Action inputs, resolved from the INPUT_* environment variables set by action.yml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

SEVERITY_ORDER = ["none", "low", "medium", "high", "critical"]

DEFAULT_EXCLUDES = [
    "**/*.lock",
    "**/package-lock.json",
    "**/node_modules/**",
]


def _get(name: str, default: str = "") -> str:
    return os.environ.get(f"INPUT_{name.upper()}", default).strip()


def _bool(name: str, default: bool = False) -> bool:
    raw = _get(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = _get(name)
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = _get(name)
    try:
        return float(raw)
    except ValueError:
        return default


def _list(name: str, default: list[str] | None = None) -> list[str]:
    """Split on both newlines and commas so either input style works."""
    raw = _get(name)
    if not raw:
        return list(default or [])
    items = [part.strip() for line in raw.splitlines() for part in line.split(",")]
    return [item for item in items if item]


@dataclass(frozen=True)
class Config:
    api_key: str
    github_token: str
    model: str
    fallback_models: list[str]
    base_url: str
    temperature: float
    max_tokens: int

    exclude: list[str] = field(default_factory=list)
    max_diff_chars: int = 180_000
    chunk_chars: int = 60_000
    max_chunks: int = 8

    skip_labels: list[str] = field(default_factory=list)
    required_labels: list[str] = field(default_factory=list)
    skip_draft: bool = True

    post_comment: bool = True
    post_inline_comments: bool = False
    comment_tag: str = "ai-pr-review"

    fail_on_severity: str = "none"
    min_score: int | None = None

    project_context: str = ""
    language: str = "English"

    @property
    def models(self) -> list[str]:
        """Primary model first, then fallbacks, de-duplicated."""
        ordered = [self.model, *self.fallback_models]
        seen: set[str] = set()
        return [m for m in ordered if m and not (m in seen or seen.add(m))]


def load() -> Config:
    api_key = _get("openrouter_api_key")
    if not api_key:
        raise SystemExit("openrouter_api_key is required")

    github_token = _get("github_token") or os.environ.get("GITHUB_TOKEN", "")
    if not github_token:
        raise SystemExit("github_token is required")

    fail_on = _get("fail_on_severity", "none").lower() or "none"
    if fail_on not in SEVERITY_ORDER:
        raise SystemExit(f"fail_on_severity must be one of {', '.join(SEVERITY_ORDER)}")

    min_score_raw = _get("min_score")
    min_score: int | None = None
    if min_score_raw:
        try:
            min_score = int(min_score_raw)
        except ValueError as exc:
            raise SystemExit("min_score must be an integer between 0 and 100") from exc

    return Config(
        api_key=api_key,
        github_token=github_token,
        model=_get("model", "anthropic/claude-sonnet-4.5"),
        fallback_models=_list("fallback_models"),
        base_url=_get("base_url", "https://openrouter.ai/api/v1").rstrip("/"),
        temperature=_float("temperature", 0.1),
        max_tokens=_int("max_tokens", 8000),
        exclude=_list("exclude", DEFAULT_EXCLUDES),
        max_diff_chars=_int("max_diff_chars", 180_000),
        chunk_chars=_int("chunk_chars", 60_000),
        max_chunks=_int("max_chunks", 8),
        skip_labels=[label.lower() for label in _list("skip_labels", ["no-ai-review"])],
        required_labels=[label.lower() for label in _list("required_labels")],
        skip_draft=_bool("skip_draft", True),
        post_comment=_bool("post_comment", True),
        post_inline_comments=_bool("post_inline_comments", False),
        comment_tag=_get("comment_tag", "ai-pr-review"),
        fail_on_severity=fail_on,
        min_score=min_score,
        project_context=os.environ.get("INPUT_PROJECT_CONTEXT", "").strip(),
        language=_get("language", "English"),
    )
