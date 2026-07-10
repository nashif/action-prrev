# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Thin GitHub REST client covering the endpoints this action needs."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from httpclient import HttpError, request

log = logging.getLogger(__name__)

USER_AGENT = "action-prrev"


@dataclass
class PullRequest:
    number: int
    title: str
    body: str
    base_ref: str
    head_ref: str
    head_sha: str
    draft: bool
    labels: list[str]
    changed_files: int
    additions: int
    deletions: int


class GitHubClient:
    def __init__(self, token: str, repo: str, api_url: str | None = None):
        self.repo = repo
        self.api_url = (api_url or os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }

    def _url(self, path: str) -> str:
        return f"{self.api_url}{path}"

    def _request(self, method: str, path: str, *, accept: str | None = None, json_body: Any = None):
        headers = dict(self._headers)
        if accept:
            headers["Accept"] = accept
        return request(method, self._url(path), headers=headers, json_body=json_body)

    def get_pull_request(self, number: int) -> PullRequest:
        data = self._request("GET", f"/repos/{self.repo}/pulls/{number}").json()
        return PullRequest(
            number=data["number"],
            title=data.get("title") or "",
            body=data.get("body") or "",
            base_ref=data["base"]["ref"],
            head_ref=data["head"]["ref"],
            head_sha=data["head"]["sha"],
            draft=bool(data.get("draft")),
            labels=[label["name"] for label in data.get("labels", [])],
            changed_files=data.get("changed_files", 0),
            additions=data.get("additions", 0),
            deletions=data.get("deletions", 0),
        )

    def get_diff(self, number: int) -> str:
        """Fetch the PR diff, falling back to reassembling per-file patches.

        GitHub refuses the `.diff` media type with 406 once a PR grows past its
        internal size limit, so the files endpoint is the safety net.
        """
        try:
            resp = self._request("GET", f"/repos/{self.repo}/pulls/{number}", accept="application/vnd.github.v3.diff")
            return resp.text
        except HttpError as exc:
            if exc.status not in (406, 422):
                raise
            log.warning("Diff media type rejected (HTTP %s); rebuilding from the files endpoint", exc.status)
            return self._diff_from_files(number)

    def _diff_from_files(self, number: int) -> str:
        parts: list[str] = []
        for file in self.list_files(number):
            path = file["filename"]
            previous = file.get("previous_filename", path)
            parts.append(f"diff --git a/{previous} b/{path}\n")
            status = file.get("status")
            if status == "added":
                parts.append("new file mode 100644\n")
            elif status == "removed":
                parts.append("deleted file mode 100644\n")
            elif status == "renamed":
                parts.append(f"rename from {previous}\nrename to {path}\n")
            patch = file.get("patch")
            if not patch:
                parts.append("Binary files differ\n")
                continue
            parts.append(f"--- a/{previous}\n+++ b/{path}\n")
            parts.append(patch if patch.endswith("\n") else patch + "\n")
        return "".join(parts)

    def list_files(self, number: int) -> list[dict[str, Any]]:
        return self._paginate(f"/repos/{self.repo}/pulls/{number}/files")

    # -- repository contents ----------------------------------------------

    def get_repo_info(self) -> dict[str, Any]:
        try:
            return self._request("GET", f"/repos/{self.repo}").json()
        except HttpError as exc:
            log.warning("Could not read repository metadata: %s", exc)
            return {}

    def get_file(self, path: str, ref: str, max_bytes: int = 400_000) -> str | None:
        """Fetch a file's contents at `ref`. Returns None when absent, binary, or oversized."""
        quoted = quote(path)
        try:
            resp = self._request(
                "GET",
                f"/repos/{self.repo}/contents/{quoted}?ref={quote(ref, safe='')}",
                accept="application/vnd.github.raw",
            )
        except HttpError as exc:
            if exc.status in (403, 404, 422):  # missing, submodule, or too large for the raw endpoint
                log.debug("No contents for %s at %s (HTTP %s)", path, ref[:7], exc.status)
                return None
            raise

        if len(resp.body) > max_bytes or b"\0" in resp.body[:8000]:
            return None
        return resp.text

    def get_tree(self, ref: str, max_entries: int = 4000) -> list[str]:
        """Return repository file paths at `ref`, empty when the tree cannot be read."""
        try:
            data = self._request("GET", f"/repos/{self.repo}/git/trees/{quote(ref, safe='')}?recursive=1").json()
        except HttpError as exc:
            log.warning("Could not read repository tree: %s", exc)
            return []
        if data.get("truncated"):
            log.info("Repository tree was truncated by the API; the overview will be partial")
        return [node["path"] for node in data.get("tree", [])[:max_entries] if node.get("type") == "blob"]

    def get_readme(self) -> str | None:
        try:
            return self._request("GET", f"/repos/{self.repo}/readme", accept="application/vnd.github.raw").text
        except HttpError:
            return None

    def _paginate(self, path: str, per_page: int = 100, max_pages: int = 30) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            sep = "&" if "?" in path else "?"
            batch = self._request("GET", f"{path}{sep}per_page={per_page}&page={page}").json()
            if not batch:
                break
            results.extend(batch)
            if len(batch) < per_page:
                break
        return results

    # -- comments ---------------------------------------------------------

    def find_comment(self, number: int, marker: str) -> dict[str, Any] | None:
        for comment in self._paginate(f"/repos/{self.repo}/issues/{number}/comments"):
            if marker in (comment.get("body") or ""):
                return comment
        return None

    def upsert_comment(self, number: int, body: str, marker: str) -> str:
        existing = self.find_comment(number, marker)
        if existing:
            resp = self._request("PATCH", f"/repos/{self.repo}/issues/comments/{existing['id']}", json_body={"body": body})
        else:
            resp = self._request("POST", f"/repos/{self.repo}/issues/{number}/comments", json_body={"body": body})
        return resp.json().get("html_url", "")

    def create_review(self, number: int, commit_sha: str, comments: list[dict[str, Any]], body: str = "") -> bool:
        """Post inline comments as a single review. Returns False if GitHub rejects them."""
        if not comments:
            return True
        payload = {"commit_id": commit_sha, "event": "COMMENT", "comments": comments}
        if body:
            payload["body"] = body
        try:
            self._request("POST", f"/repos/{self.repo}/pulls/{number}/reviews", json_body=payload)
            return True
        except HttpError as exc:
            log.warning("Inline review rejected: %s", exc)
            return False


def event_payload() -> dict[str, Any]:
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def pull_request_number() -> int | None:
    payload = event_payload()
    for key in ("pull_request", "issue"):
        node = payload.get(key)
        if isinstance(node, dict) and "number" in node:
            return int(node["number"])
    number = os.environ.get("PR_NUMBER")
    return int(number) if number and number.isdigit() else None


def repo_slug() -> str:
    slug = os.environ.get("GITHUB_REPOSITORY", "")
    if not slug:
        raise SystemExit("GITHUB_REPOSITORY is not set; this action must run inside GitHub Actions")
    return slug


def file_url(repo: str, sha: str, path: str, line: int | None = None) -> str:
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    url = f"{server}/{repo}/blob/{sha}/{quote(path)}"
    return f"{url}#L{line}" if line else url
