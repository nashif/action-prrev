# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""OpenRouter chat-completions client with structured-output handling."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from httpclient import HttpError, request

log = logging.getLogger(__name__)

FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL)


@dataclass
class Completion:
    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float | None = None


def _header_safe(value: str) -> str:
    """urllib encodes header values as latin-1, so strip anything it cannot represent."""
    return value.encode("ascii", errors="ignore").decode("ascii").strip() or "AI PR Review"


class OpenRouterClient:
    def __init__(self, api_key: str, base_url: str, *, referer: str = "", title: str = "AI PR Review"):
        self.base_url = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # OpenRouter attributes usage to these headers; both are optional.
            "HTTP-Referer": _header_safe(referer or "https://github.com/features/actions"),
            "X-Title": _header_safe(title),
        }

    def complete(
        self,
        *,
        models: list[str],
        system: str,
        user: str,
        temperature: float,
        max_tokens: int,
        json_schema: dict[str, Any] | None = None,
    ) -> Completion:
        """Try each model in order; the first that answers wins."""
        errors: list[str] = []
        for model in models:
            try:
                return self._complete_once(model, system, user, temperature, max_tokens, json_schema)
            except (HttpError, ValueError) as exc:
                log.warning("Model %s failed: %s", model, exc)
                errors.append(f"{model}: {exc}")
        raise RuntimeError("All models failed:\n" + "\n".join(errors))

    def _complete_once(
        self,
        model: str,
        system: str,
        user: str,
        temperature: float,
        max_tokens: int,
        json_schema: dict[str, Any] | None,
    ) -> Completion:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "usage": {"include": True},
        }
        if json_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "pr_review", "strict": True, "schema": json_schema},
            }

        try:
            resp = self._post(payload)
        except HttpError as exc:
            # Not every model supports structured outputs; retry unconstrained.
            if json_schema and exc.status in (400, 404, 422):
                log.warning("%s rejected json_schema response_format; retrying with a plain request", model)
                payload.pop("response_format", None)
                resp = self._post(payload)
            else:
                raise

        data = resp.json()
        if "error" in data and not data.get("choices"):
            raise ValueError(str(data["error"]))

        choices = data.get("choices") or []
        if not choices:
            raise ValueError("no choices returned")
        message = choices[0].get("message") or {}
        content = (message.get("content") or "").strip()
        if not content:
            reason = choices[0].get("finish_reason")
            raise ValueError(f"empty response (finish_reason={reason})")

        usage = data.get("usage") or {}
        return Completion(
            content=content,
            model=data.get("model", model),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            cost=usage.get("cost"),
        )

    def _post(self, payload: dict[str, Any]):
        return request(
            "POST",
            f"{self.base_url}/chat/completions",
            headers=self._headers,
            json_body=payload,
            timeout=300,
        )


def parse_json(content: str) -> dict[str, Any]:
    """Recover a JSON object from a model response that may be fenced or prefaced."""
    content = content.strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    fenced = FENCE_RE.search(content)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass

    start = content.find("{")
    end = content.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(content[start : end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError(f"model did not return valid JSON: {content[:400]}")
