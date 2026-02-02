"""
Scaledown LLM client (minimal, provider-agnostic adapter).

Implements the `LLMClient` Protocol defined in `reasoning/insight_reasoner.py`.

Design goals:
- Minimal surface area: `complete()` + optional `embed()`
- Environment-based configuration (no secrets in code)
- Stdlib-only HTTP (no heavy dependencies)
- Provider-agnostic payload shape with sensible defaults

Environment variables:
- SCALEDOWN_API_KEY (required)
- SCALEDOWN_BASE_URL (optional, default: https://api.scaledown.ai)
- SCALEDOWN_CHAT_PATH (optional, default: /v1/chat/completions)
- SCALEDOWN_EMBED_PATH (optional, default: /v1/embeddings)
- SCALEDOWN_MODEL (optional, default: gpt-4.1-mini)  # example default
- SCALEDOWN_TIMEOUT_SECONDS (optional, default: 30)

Notes:
- The exact API schema may differ by provider; this client is intentionally small
  and tolerant. If the response shape is unknown, it raises a clear error.
- `embed()` is optional: if the endpoint fails (e.g., 404), it returns None.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence


class ScaledownClientError(RuntimeError):
    """Raised when the Scaledown client cannot complete a request successfully."""


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v


@dataclass
class ScaledownClientConfig:
    api_key: str
    base_url: str = "https://api.scaledown.ai"
    chat_path: str = "/v1/chat/completions"
    embed_path: str = "/v1/embeddings"
    model: str = "gpt-4.1-mini"
    timeout_seconds: int = 30


class ScaledownLLMClient:
    """
    Minimal client implementing:
    - complete(prompt, temperature) -> str
    - embed(texts) -> Optional[List[List[float]]]
    """

    def __init__(self, config: Optional[ScaledownClientConfig] = None) -> None:
        if config is None:
            api_key = _env("SCALEDOWN_API_KEY")
            if not api_key:
                raise ScaledownClientError("Missing env var SCALEDOWN_API_KEY")
            config = ScaledownClientConfig(
                api_key=api_key,
                base_url=_env("SCALEDOWN_BASE_URL", "https://api.scaledown.ai") or "https://api.scaledown.ai",
                chat_path=_env("SCALEDOWN_CHAT_PATH", "/v1/chat/completions") or "/v1/chat/completions",
                embed_path=_env("SCALEDOWN_EMBED_PATH", "/v1/embeddings") or "/v1/embeddings",
                model=_env("SCALEDOWN_MODEL", "gpt-4.1-mini") or "gpt-4.1-mini",
                timeout_seconds=int(_env("SCALEDOWN_TIMEOUT_SECONDS", "30") or "30"),
            )
        self.config = config

    # ---- Public API (matches reasoning.insight_reasoner.LLMClient Protocol) ----

    def complete(self, *, prompt: str, temperature: float = 0.2) -> str:
        """
        Execute a single completion call.

        Payload follows a common "chat completions" pattern:
        { model, messages:[{role:"user",content:prompt}], temperature }
        """
        url = self._url(self.config.chat_path)
        payload: Dict[str, Any] = {
            "model": self.config.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": float(temperature),
        }
        data = self._post_json(url, payload)
        return self._extract_text(data)

    def embed(self, texts: Sequence[str]) -> Optional[List[List[float]]]:
        """
        Optional embedding support.

        Returns:
            List of vectors aligned to `texts`, or None if embeddings are unavailable.
        """
        url = self._url(self.config.embed_path)
        payload: Dict[str, Any] = {
            "model": self.config.model,
            "input": list(texts),
        }
        try:
            data = self._post_json(url, payload)
        except ScaledownClientError as e:
            # If the provider doesn't support embeddings (or endpoint differs), treat as optional.
            msg = str(e).lower()
            if "404" in msg or "not found" in msg:
                return None
            return None
        return self._extract_embeddings(data)

    # ---- HTTP helpers ----

    def _url(self, path: str) -> str:
        base = (self.config.base_url or "").rstrip("/")
        p = (path or "").strip()
        if not p.startswith("/"):
            p = "/" + p
        return base + p

    def _post_json(self, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url=url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
                raw = resp.read().decode("utf-8")
                try:
                    return json.loads(raw) if raw.strip() else {}
                except json.JSONDecodeError as je:
                    raise ScaledownClientError(f"Non-JSON response from LLM API: {je}") from je
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")
            except Exception:
                detail = ""
            raise ScaledownClientError(f"HTTP {e.code} from LLM API: {detail or e.reason}") from e
        except urllib.error.URLError as e:
            raise ScaledownClientError(f"Network error calling LLM API: {e}") from e

    # ---- Response extractors (tolerant to common shapes) ----

    @staticmethod
    def _extract_text(data: Dict[str, Any]) -> str:
        """
        Try to extract the assistant text from common response shapes:
        - OpenAI-like: {choices:[{message:{content:"..."}}]}
        - Completion-like: {choices:[{text:"..."}]}
        - Direct: {output_text:"..."} or {content:"..."}
        """
        if not isinstance(data, dict):
            raise ScaledownClientError("Unexpected response type (expected JSON object).")

        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0] or {}
            if isinstance(first, dict):
                msg = first.get("message")
                if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                    return msg["content"]
                if isinstance(first.get("text"), str):
                    return first["text"]

        for k in ("output_text", "content", "result"):
            v = data.get(k)
            if isinstance(v, str):
                return v

        raise ScaledownClientError("Unable to extract completion text from LLM response.")

    @staticmethod
    def _extract_embeddings(data: Dict[str, Any]) -> Optional[List[List[float]]]:
        """
        Try to extract embeddings from common response shapes:
        - OpenAI-like: {data:[{embedding:[...]} , ...]}
        - Direct: {embeddings:[[...], ...]}
        """
        if not isinstance(data, dict):
            return None

        if isinstance(data.get("embeddings"), list):
            emb = data["embeddings"]
            if all(isinstance(v, list) for v in emb):
                return emb  # type: ignore[return-value]

        items = data.get("data")
        if isinstance(items, list) and items:
            out: List[List[float]] = []
            for it in items:
                if isinstance(it, dict) and isinstance(it.get("embedding"), list):
                    out.append(it["embedding"])  # type: ignore[arg-type]
            return out if out else None

        return None

