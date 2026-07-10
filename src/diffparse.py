# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Unified-diff parsing, file filtering, and chunking for oversized pull requests."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatch

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
BINARY_MARKERS = ("GIT binary patch", "Binary files ")


@dataclass
class FileDiff:
    path: str
    patch: str
    old_path: str | None = None
    status: str = "modified"
    binary: bool = False
    additions: int = 0
    deletions: int = 0
    # New-side line numbers that were added or kept in context; only these can
    # legally carry an inline review comment.
    commentable_lines: set[int] = field(default_factory=set)

    @property
    def size(self) -> int:
        return len(self.patch)


def parse(diff_text: str) -> list[FileDiff]:
    """Split a `git diff` / GitHub `.diff` payload into per-file records."""
    files: list[FileDiff] = []
    for block in _split_file_blocks(diff_text):
        parsed = _parse_file_block(block)
        if parsed is not None:
            files.append(parsed)
    return files


def _split_file_blocks(diff_text: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    for line in diff_text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current:
                blocks.append("".join(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append("".join(current))
    return blocks


def _parse_file_block(block: str) -> FileDiff | None:
    lines = block.splitlines()
    header = lines[0]
    match = re.match(r'^diff --git "?a/(.+?)"? "?b/(.+?)"?$', header)
    if not match:
        return None
    old_path, new_path = match.group(1), match.group(2)

    status = "modified"
    binary = any(any(line.startswith(m) for m in BINARY_MARKERS) for line in lines)
    additions = deletions = 0
    commentable: set[int] = set()
    new_line = 0

    for line in lines[1:]:
        if line.startswith("new file mode"):
            status = "added"
        elif line.startswith("deleted file mode"):
            status = "removed"
        elif line.startswith("rename from"):
            status = "renamed"
        elif line.startswith("@@"):
            hunk = HUNK_RE.match(line)
            if hunk:
                new_line = int(hunk.group(3))
        elif line.startswith("+++") or line.startswith("---"):
            continue
        elif line.startswith("+"):
            additions += 1
            commentable.add(new_line)
            new_line += 1
        elif line.startswith("-"):
            deletions += 1
        elif line.startswith(" "):
            new_line += 1

    path = new_path if status != "removed" else old_path
    return FileDiff(
        path=path,
        patch=block,
        old_path=old_path if old_path != new_path else None,
        status=status,
        binary=binary,
        additions=additions,
        deletions=deletions,
        commentable_lines=commentable,
    )


def is_excluded(path: str, patterns: list[str]) -> bool:
    """Glob match that treats a leading `**/` as optional, the way .gitignore users expect."""
    basename = path.rsplit("/", 1)[-1]
    for pattern in patterns:
        candidates = [pattern]
        if pattern.startswith("**/"):
            candidates.append(pattern[3:])
        for candidate in candidates:
            if fnmatch(path, candidate) or fnmatch(basename, candidate):
                return True
            # `dir/**` should also match the directory's direct children.
            if candidate.endswith("/**") and fnmatch(path, candidate[:-3] + "/*"):
                return True
    return False


def filter_files(files: list[FileDiff], exclude: list[str]) -> tuple[list[FileDiff], list[str]]:
    """Return reviewable files plus the paths that were dropped."""
    kept: list[FileDiff] = []
    dropped: list[str] = []
    for file in files:
        if file.binary or is_excluded(file.path, exclude):
            dropped.append(file.path)
        else:
            kept.append(file)
    return kept, dropped


def _split_oversized(file: FileDiff, limit: int) -> list[str]:
    """Break one file's patch along hunk boundaries so no single piece exceeds `limit`."""
    lines = file.patch.splitlines(keepends=True)
    head: list[str] = []
    hunks: list[list[str]] = []
    for line in lines:
        if line.startswith("@@"):
            hunks.append([line])
        elif hunks:
            hunks[-1].append(line)
        else:
            head.append(line)

    if not hunks:
        return [file.patch[:limit]]

    header = "".join(head)
    pieces: list[str] = []
    current: list[str] = []
    current_size = len(header)
    for hunk in hunks:
        hunk_text = "".join(hunk)
        if current and current_size + len(hunk_text) > limit:
            pieces.append(header + "".join(current))
            current, current_size = [], len(header)
        current.append(hunk_text)
        current_size += len(hunk_text)
    if current:
        pieces.append(header + "".join(current))
    return pieces


def chunk(files: list[FileDiff], chunk_chars: int, max_chunks: int) -> tuple[list[str], bool]:
    """Pack file patches into diff chunks, each roughly `chunk_chars` long.

    Files stay whole unless a single file is larger than the budget, in which
    case it is divided at hunk boundaries. The bool reports whether `max_chunks`
    forced some of the diff to be dropped.
    """
    pieces: list[str] = []
    for file in files:
        if file.size > chunk_chars:
            pieces.extend(_split_oversized(file, chunk_chars))
        else:
            pieces.append(file.patch)

    chunks: list[str] = []
    current: list[str] = []
    current_size = 0
    for piece in pieces:
        if current and current_size + len(piece) > chunk_chars:
            chunks.append("".join(current))
            current, current_size = [], 0
        current.append(piece)
        current_size += len(piece)
    if current:
        chunks.append("".join(current))

    return chunks[:max_chunks], len(chunks) > max_chunks


def commentable_map(files: list[FileDiff]) -> dict[str, set[int]]:
    return {file.path: file.commentable_lines for file in files}
