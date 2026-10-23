"""A small client for OmniRoute's OpenAI-compatible chat API."""

from __future__ import annotations

from typing import Protocol

import httpx


class LlmError(Exception):
    pass


class Llm(Protocol):
    def chat(self, messages: list[dict]) -> str: ...


class OmniRoute:
    def __init__(self, url: str, key: str, model: str, timeout: float = 120.0) -> None:
        self.url, self.key, self.model, self.timeout = url.rstrip("/"), key, model, timeout

    def models(self) -> list[str]:
        data = self._call("GET", "/models")
        return sorted(m["id"] for m in data.get("data", []))

    def chat(self, messages: list[dict]) -> str:
        model = self.model or (self.models() or [""])[0]
        if not model:
            raise LlmError("OmniRoute lists no models; pick one in Settings")
        data = self._call("POST", "/chat/completions",
                          {"model": model, "messages": messages, "temperature": 0.2})  # fmt: skip
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as error:
            raise LlmError(f"unexpected reply from OmniRoute: {str(data)[:300]}") from error

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        if not self.key:
            raise LlmError("no OmniRoute key yet: add it in Settings")
        try:
            response = httpx.request(method, self.url + path, json=body, timeout=self.timeout,
                                     headers={"Authorization": f"Bearer {self.key}"})  # fmt: skip
        except httpx.HTTPError as error:
            raise LlmError(
                f"can't reach OmniRoute at {self.url} ({error.__class__.__name__}). Is the SSH "
                "tunnel up? The start script opens it."
            ) from error
        if response.status_code >= 400:
            raise LlmError(f"OmniRoute said {response.status_code}: {response.text[:300]}")
        return response.json()
