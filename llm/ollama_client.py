"""
Ollama local LLM client.

Implements the `LLMClient` Protocol defined in `reasoning/insight_reasoner.py`.

Design goals:
- Minimal, explicit HTTP calls to local Ollama
- No external dependencies beyond stdlib
- Model: llama3.1:8b (fully local, no API keys)
- complete(prompt) -> str
- No embeddings (optional in Protocol)

Environment variables:
- OLLAMA_BASE_URL (optional, default: http://localhost:11434)
- OLLAMA_MODEL (optional, default: llama3.1:8b)
- OLLAMA_TIMEOUT_SECONDS (optional, default: 120)
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence


class OllamaClientError(RuntimeError):
    """Raised when the Ollama client cannot complete a request successfully."""


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v


@dataclass
class OllamaClientConfig:
    base_url: str = "http://localhost:11434"
    model: str = "llama3.1:8b"
    timeout_seconds: int = 120


class OllamaLLMClient:
    """
    Local Ollama LLM client.

    Requirements:
    - Ollama running on http://localhost:11434 (or OLLAMA_BASE_URL)
    - Model llama3.1:8b pre-pulled (or OLLAMA_MODEL)
    """

    def __init__(self, config: Optional[OllamaClientConfig] = None) -> None:
        if config is None:
            config = OllamaClientConfig(
                base_url=_env("OLLAMA_BASE_URL", "http://localhost:11434") or "http://localhost:11434",
                model=_env("OLLAMA_MODEL", "llama3.1:8b") or "llama3.1:8b",
                timeout_seconds=int(_env("OLLAMA_TIMEOUT_SECONDS", "120") or "120"),
            )
        self.config = config

    # ---- Public API (matches reasoning.insight_reasoner.LLMClient Protocol) ----

    def complete(self, *, prompt: str, temperature: float = 0.2) -> str:
        """
        Execute a completion call against local Ollama.

        Args:
            prompt: Full prompt text
            temperature: Sampling temperature (0.0 = deterministic, 1.0 = creative)

        Returns:
            Generated response string
        """
        url = f"{self.config.base_url.rstrip('/')}/api/generate"
        payload: Dict[str, Any] = {
            "model": self.config.model,
            "prompt": prompt,
            "temperature": float(temperature),
            "stream": False,  # Wait for complete response
        }

        try:
            body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                url=url,
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )

            with urllib.request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
                raw = resp.read().decode("utf-8")
                try:
                    data = json.loads(raw) if raw.strip() else {}
                except json.JSONDecodeError as je:
                    raise OllamaClientError(f"Non-JSON response from Ollama: {je}") from je

                response_text = data.get("response", "").strip()
                if not response_text:
                    raise OllamaClientError("Empty response from Ollama")
                return response_text

        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")
            except Exception:
                pass
            raise OllamaClientError(f"HTTP {e.code} from Ollama: {detail or e.reason}") from e
        except urllib.error.URLError as e:
            raise OllamaClientError(
                f"Cannot connect to Ollama at {url}. Ensure Ollama is running. Error: {e}"
            ) from e
        except Exception as e:
            raise OllamaClientError(f"Error calling Ollama: {e}") from e

    def embed(self, texts: Sequence[str]) -> Optional[List[List[float]]]:
        """
        Embeddings not supported by this local client.
        Returns None to signal that embedding-based dedup should be skipped.
        """
        return None
