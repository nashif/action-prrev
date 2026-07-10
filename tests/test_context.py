# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

import context
import diffparse

SOURCE = "\n".join(f"line {n}" for n in range(1, 101))

PY_SOURCE = '''import os


def helper(value):
    return value * 2


class Service:
    def __init__(self):
        self.cache = {}

    def login(self, token):
        return self.cache.get(token)


def unrelated():
    pass
'''


class FakeGitHub:
    """Stands in for GitHubClient with just the methods ContextBuilder touches."""

    def __init__(self, files=None, info=None, tree=None, readme=None):
        self.files = files or {}
        self.info = info or {}
        self.tree = tree or []
        self.readme = readme
        self.reads: list[str] = []

    def get_file(self, path, ref, max_bytes=400_000):
        self.reads.append(path)
        return self.files.get(path)

    def get_repo_info(self):
        return self.info

    def get_tree(self, ref, max_entries=4000):
        return self.tree

    def get_readme(self):
        return self.readme


# -- window math -------------------------------------------------------------


def test_windows_expand_by_padding_and_clamp_to_the_file():
    assert context.merge_windows([(10, 12)], padding=5, total=100) == [(5, 17)]
    assert context.merge_windows([(2, 3)], padding=10, total=100) == [(1, 13)]
    assert context.merge_windows([(95, 100)], padding=10, total=100) == [(85, 100)]


def test_overlapping_windows_merge():
    assert context.merge_windows([(10, 12), (20, 22)], padding=5, total=100) == [(5, 27)]


def test_distant_windows_stay_separate():
    assert context.merge_windows([(10, 12), (80, 82)], padding=5, total=100) == [(5, 17), (75, 87)]


def test_no_ranges_yields_no_windows():
    assert context.merge_windows([], padding=5, total=100) == []


# -- rendering ---------------------------------------------------------------


def test_rendered_context_carries_real_line_numbers():
    block = context.render_file_context("f.txt", SOURCE, [(50, 51)], padding=2)
    assert "   48| line 48" in block
    assert "   53| line 53" in block
    assert "   47| line 47" not in block


def test_rendered_context_marks_omitted_regions():
    block = context.render_file_context("f.txt", SOURCE, [(50, 50)], padding=1)
    assert "# ... 48 line(s) omitted ..." in block  # lines 1-48 before the window
    assert "# ... 49 line(s) omitted ..." in block  # lines 52-100 after it


def test_small_file_is_shown_whole_without_gap_markers():
    block = context.render_file_context("f.py", "a\nb\nc\n", [(2, 2)], padding=10)
    assert "omitted" not in block
    assert "    1| a" in block and "    3| c" in block


def test_outline_lists_definitions_outside_the_window():
    block = context.render_file_context("s.py", PY_SOURCE, [(11, 12)], padding=1)
    assert "Other definitions in this file:" in block
    assert "def helper(value):" in block
    assert "class Service:" in block
    # login() is inside the shown window, so it is not repeated in the outline.
    outline = block.split("#\n")[0]
    assert "def login" not in outline


def test_header_reports_the_true_file_length():
    assert "(100 lines)" in context.render_file_context("f.txt", SOURCE, [(1, 1)], padding=0)


# -- builder -----------------------------------------------------------------


def _file(path, ranges, status="modified"):
    return diffparse.FileDiff(path=path, patch="", status=status, hunk_ranges=ranges)


def test_builder_fetches_and_caches_each_file_once():
    gh = FakeGitHub(files={"a.py": PY_SOURCE})
    builder = context.ContextBuilder(gh, "acme/app", "sha")
    files = [_file("a.py", [(11, 12)])]

    builder.file_context(files, padding=2, max_chars=10_000)
    builder.file_context(files, padding=2, max_chars=10_000)
    assert gh.reads == ["a.py"]


def test_builder_skips_deleted_files_and_files_without_hunks():
    gh = FakeGitHub(files={"gone.py": PY_SOURCE})
    builder = context.ContextBuilder(gh, "acme/app", "sha")
    out = builder.file_context([_file("gone.py", [(1, 2)], status="removed"), _file("x.py", [])], 2, 10_000)
    assert out == ""
    assert gh.reads == []


def test_builder_notes_files_it_could_not_read():
    gh = FakeGitHub(files={"a.py": PY_SOURCE})  # b.py returns None
    builder = context.ContextBuilder(gh, "acme/app", "sha")
    out = builder.file_context([_file("a.py", [(11, 12)]), _file("b.py", [(1, 2)])], 2, 10_000)
    assert "`a.py`" in out
    assert "Context omitted for 1 file(s)" in out


def test_context_budget_stops_before_crowding_out_the_diff():
    gh = FakeGitHub(files={"a.py": SOURCE, "b.py": SOURCE})
    builder = context.ContextBuilder(gh, "acme/app", "sha")
    # Budget for exactly one rendered block, so the second file must be dropped.
    one_block = len(context.render_file_context("a.py", SOURCE, [(50, 51)], padding=5)) + 2

    out = builder.file_context([_file("a.py", [(50, 51)]), _file("b.py", [(50, 51)])], padding=5, max_chars=one_block)
    assert "`a.py`" in out
    assert "`b.py`" not in out
    assert "Context omitted for 1 file(s)" in out


def test_a_file_too_large_for_the_whole_budget_is_dropped_not_truncated():
    """Half a function is worse than none: it invites findings about code that is really there."""
    gh = FakeGitHub(files={"a.py": SOURCE})
    builder = context.ContextBuilder(gh, "acme/app", "sha")
    out = builder.file_context([_file("a.py", [(50, 51)])], padding=40, max_chars=100)
    assert out == ""


def test_repo_overview_summarizes_metadata_layout_and_readme():
    gh = FakeGitHub(
        info={"description": "A payments service.", "language": "Python", "topics": ["fintech"]},
        tree=["README.md", "pyproject.toml", "src/api/routes.py", "src/api/auth.py", "src/db/models.py"],
        readme="# Payments\n\nHandles money.",
    )
    overview = context.ContextBuilder(gh, "acme/app", "sha").repo_overview()
    assert "A payments service." in overview
    assert "Primary language: Python" in overview
    assert "Topics: fintech" in overview
    assert "`src/api/` — 2 file(s)" in overview
    assert "`pyproject.toml`" in overview
    assert "Handles money." in overview


def test_repo_overview_is_cached():
    class CountingGitHub(FakeGitHub):
        calls = 0

        def get_repo_info(self):
            CountingGitHub.calls += 1
            return {}

    gh = CountingGitHub()
    builder = context.ContextBuilder(gh, "acme/app", "sha")
    builder.repo_overview()
    builder.repo_overview()
    assert CountingGitHub.calls == 1


def test_repo_overview_survives_a_bare_repository():
    assert context.ContextBuilder(FakeGitHub(), "acme/app", "sha").repo_overview() == ""


def test_workspace_source_reads_from_disk(tmp_path):
    (tmp_path / "a.py").write_text(PY_SOURCE)
    gh = FakeGitHub()
    builder = context.ContextBuilder(gh, "acme/app", "sha", source="workspace", workspace=str(tmp_path))
    out = builder.file_context([_file("a.py", [(11, 12)])], 2, 10_000)
    assert "def login" in out
    assert gh.reads == []  # never touched the API


def test_workspace_source_refuses_to_escape_the_workspace(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (tmp_path / "secret.txt").write_text("token")
    builder = context.ContextBuilder(FakeGitHub(), "acme/app", "sha", source="workspace", workspace=str(workspace))
    assert builder.read("../secret.txt") is None
