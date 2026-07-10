# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Review profiles: the domain half of the system prompt.

A profile says what kind of reviewer to be and what to look for. It never says
how to format the answer — `prompts.OUTPUT_CONTRACT` owns that, and is appended
to every profile so a profile can never break JSON parsing.

Built-in profiles live in `review_profiles/*.md`. A repository can supply its own
by pointing `review_profile` at a file path instead of a name.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

PROFILE_DIR = Path(__file__).resolve().parent.parent / "review_profiles"
MAX_PROFILE_CHARS = 60_000


def available() -> list[str]:
    """Names of the built-in profiles, alphabetically."""
    if not PROFILE_DIR.is_dir():
        return []
    return sorted(path.stem for path in PROFILE_DIR.glob("*.md"))


def _resolve_path(value: str) -> Path | None:
    """Interpret `value` as a path to a profile file, relative to the workspace if needed."""
    candidate = Path(value)
    if candidate.is_file():
        return candidate
    workspace = os.environ.get("GITHUB_WORKSPACE")
    if workspace:
        in_workspace = Path(workspace) / value
        if in_workspace.is_file():
            return in_workspace
    return None


def load(value: str) -> str:
    """Return the profile text for a built-in name or a path to a Markdown file."""
    value = (value or "default").strip()

    builtin = PROFILE_DIR / f"{value}.md"
    if builtin.is_file():
        return _read(builtin)

    # Only fall back to the filesystem when the value actually looks like a path.
    # A bare typo ("zepyhr") should fail loudly, not be searched for on disk.
    if "/" in value or value.endswith(".md"):
        path = _resolve_path(value)
        if path is not None:
            log.info("Using custom review profile from %s", path)
            return _read(path)
        raise SystemExit(f"review_profile file not found: {value}")

    raise SystemExit(f"unknown review_profile {value!r}; available profiles: {', '.join(available()) or 'none'}")


def _read(path: Path) -> str:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise SystemExit(f"review profile {path} is empty")
    if len(text) > MAX_PROFILE_CHARS:
        raise SystemExit(f"review profile {path} exceeds {MAX_PROFILE_CHARS} characters")
    return text
