# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Builds the codebase context that surrounds a diff.

A diff alone hides the things a reviewer needs: what the enclosing function
promises, whether a caller already validated the argument, what the file's other
branches do. This module fetches the real files at the pull request's head commit
and shows the model the code around each hunk, plus a short map of the repository.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

from diffparse import FileDiff

log = logging.getLogger(__name__)

# Lines that outline a file's structure across the languages people actually
# open pull requests in. Used to orient the model outside the shown windows.
OUTLINE_RE = re.compile(
    r"^\s*(?:"
    r"(?:async\s+)?def\s+\w+|"
    r"class\s+\w+|"
    r"(?:export\s+)?(?:async\s+)?function\s+\w+|"
    r"(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+\w+|"
    r"func\s+(?:\([^)]*\)\s*)?\w+|"
    r"fn\s+\w+|(?:pub\s+)?(?:struct|enum|trait|impl)\s+\w+|"
    r"(?:public|private|protected|internal)\s+[\w<>\[\],\s]+\s+\w+\s*\(|"
    r"(?:export\s+)?(?:const|let|var)\s+\w+\s*=\s*(?:async\s*)?\(|"
    r"(?:type|interface|module|package)\s+\w+"
    r")"
)

DOC_FILES = ("README.md", "README.rst", "README", "CONTRIBUTING.md", "ARCHITECTURE.md")
MANIFESTS = (
    "pyproject.toml", "setup.py", "requirements.txt", "package.json", "tsconfig.json",
    "go.mod", "Cargo.toml", "pom.xml", "build.gradle", "Gemfile", "composer.json",
    "Dockerfile", "docker-compose.yml", "Makefile",
)


@dataclass
class Budget:
    """A shrinking character allowance, so context can never crowd out the diff."""

    remaining: int

    def take(self, text: str) -> str | None:
        if self.remaining <= 0:
            return None
        if len(text) > self.remaining:
            return None
        self.remaining -= len(text)
        return text


def merge_windows(ranges: list[tuple[int, int]], padding: int, total: int) -> list[tuple[int, int]]:
    """Expand each hunk range by `padding` lines and merge the ones that overlap."""
    if not ranges:
        return []
    expanded = sorted((max(1, start - padding), min(total, end + padding)) for start, end in ranges)
    merged = [expanded[0]]
    for start, end in expanded[1:]:
        last_start, last_end = merged[-1]
        # Touching windows (a one-line gap) are worth merging too; the gap marker
        # would cost more lines than the code it hides.
        if start <= last_end + 2:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _outline(lines: list[str], shown: list[tuple[int, int]], limit: int = 25) -> list[str]:
    """Definition lines that fall outside the shown windows, so structure is not lost."""
    covered = {number for start, end in shown for number in range(start, end + 1)}
    found: list[str] = []
    for number, text in enumerate(lines, start=1):
        if number in covered or not OUTLINE_RE.match(text):
            continue
        found.append(f"{number:>5}| {text.rstrip()[:120]}")
        if len(found) >= limit:
            break
    return found


def render_file_context(path: str, source: str, ranges: list[tuple[int, int]], padding: int) -> str:
    """Render the numbered slices of `source` that surround the diff's hunks."""
    lines = source.splitlines()
    total = len(lines)
    windows = merge_windows(ranges, padding, total)
    if not windows:
        return ""

    out = [f"#### `{path}` ({total} lines)", "```"]
    outline = _outline(lines, windows)
    if outline:
        out += ["# Other definitions in this file:", *outline, "#"]

    previous_end = 0
    for start, end in windows:
        if start > previous_end + 1:
            hidden = start - previous_end - 1
            out.append(f"# ... {hidden} line(s) omitted ...")
        for number in range(start, end + 1):
            out.append(f"{number:>5}| {lines[number - 1].rstrip()}")
        previous_end = end
    if previous_end < total:
        out.append(f"# ... {total - previous_end} line(s) omitted ...")
    out.append("```")
    return "\n".join(out)


class ContextBuilder:
    """Fetches surrounding code, from the workspace checkout or the GitHub API."""

    def __init__(self, gh, repo: str, ref: str, *, source: str = "api", workspace: str | None = None):
        self.gh = gh
        self.repo = repo
        self.ref = ref
        self.source = source
        self.workspace = workspace or os.environ.get("GITHUB_WORKSPACE", "")
        self._cache: dict[str, str | None] = {}
        self._overview: str | None = None

    def read(self, path: str) -> str | None:
        if path not in self._cache:
            self._cache[path] = self._read_uncached(path)
        return self._cache[path]

    def _read_uncached(self, path: str) -> str | None:
        if self.source == "workspace" and self.workspace:
            local = os.path.join(self.workspace, path)
            # Refuse to escape the workspace if a diff ever carries a `..` path.
            if os.path.commonpath([os.path.realpath(local), os.path.realpath(self.workspace)]) != os.path.realpath(
                self.workspace
            ):
                return None
            try:
                with open(local, encoding="utf-8") as handle:
                    return handle.read()
            except (OSError, UnicodeDecodeError):
                return None
        try:
            return self.gh.get_file(path, self.ref)
        except Exception as exc:  # noqa: BLE001 - context is best-effort, never fatal
            log.warning("Could not read %s for context: %s", path, exc)
            return None

    # -- repository overview ----------------------------------------------

    def repo_overview(self, max_chars: int = 6000) -> str:
        """A cached, one-screen map of the repository: what it is and how it is laid out."""
        if self._overview is not None:
            return self._overview

        parts: list[str] = []
        info = self.gh.get_repo_info()
        facts = []
        if info.get("description"):
            facts.append(info["description"].strip())
        if info.get("language"):
            facts.append(f"Primary language: {info['language']}.")
        if info.get("topics"):
            facts.append(f"Topics: {', '.join(info['topics'][:8])}.")
        if facts:
            parts.append(" ".join(facts))

        paths = self.gh.get_tree(self.ref)
        if paths:
            parts.append(self._layout(paths))
            manifests = [p for p in paths if p in MANIFESTS or p.rsplit("/", 1)[-1] in MANIFESTS]
            if manifests:
                parts.append("Build/dependency manifests: " + ", ".join(f"`{p}`" for p in sorted(manifests)[:12]))

        readme = self.gh.get_readme()
        if readme:
            excerpt = readme.strip()[:1500]
            parts.append(f"README excerpt:\n```\n{excerpt}\n```")

        self._overview = "\n\n".join(parts)[:max_chars]
        return self._overview

    @staticmethod
    def _layout(paths: list[str], limit: int = 30) -> str:
        """Top-level and second-level directories with file counts."""
        counts: dict[str, int] = {}
        root_files: list[str] = []
        for path in paths:
            head, _, tail = path.partition("/")
            if not tail:
                root_files.append(head)
                continue
            second = tail.split("/", 1)[0] if "/" in tail else ""
            key = f"{head}/{second}" if second and "." not in second else head
            counts[key] = counts.get(key, 0) + 1

        top = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
        lines = [f"Repository layout ({len(paths)} files):"]
        lines += [f"- `{name}/` — {count} file(s)" for name, count in top]
        if root_files:
            lines.append("- root: " + ", ".join(f"`{name}`" for name in sorted(root_files)[:12]))
        return "\n".join(lines)

    # -- per-file context ---------------------------------------------------

    def file_context(self, files: list[FileDiff], padding: int, max_chars: int) -> str:
        """Surrounding source for every file in this slice, newest-first until the budget runs out."""
        budget = Budget(max_chars)
        blocks: list[str] = []
        skipped: list[str] = []

        for file in files:
            if file.status == "removed" or not file.hunk_ranges:
                continue
            source = self.read(file.path)
            if source is None:
                skipped.append(file.path)
                continue
            block = render_file_context(file.path, source, file.hunk_ranges, padding)
            if not block:
                continue
            taken = budget.take(block + "\n\n")
            if taken is None:
                skipped.append(file.path)
                continue
            blocks.append(taken)

        if not blocks:
            return ""
        rendered = "".join(blocks).rstrip()
        if skipped:
            rendered += f"\n\n> Context omitted for {len(skipped)} file(s) (unavailable or over the context budget)."
        return rendered
