"""
ScaleDown Prompt Compression Client.

ONLY for prompt compression, NOT for answer generation.

Design goals:
- Minimal, explicit: compress(text) -> compressed_text
- Endpoint: https://api.scaledown.xyz/compress/raw/
- Header: x-api-key
- Reduces token count without generating answers

Environment variables:
- SCALEDOWN_API_KEY (required)
- SCALEDOWN_BASE_URL (optional, default: https://api.scaledown.xyz)
- SCALEDOWN_TIMEOUT_SECONDS (optional, default: 30)

Notes:
- This client is ONLY a compression tool, not an LLM client.
- It does not implement the LLMClient Protocol.
- Used by reasoning layer to compress prompts before sending to actual LLM (e.g., Ollama).
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
    base_url: str = "https://api.scaledown.xyz"
    timeout_seconds: int = 30


class ScaledownCompressionClient:
    """
    Compress prompts using ScaleDown API.

    ONLY for compression - does NOT generate answers.
    Endpoint: https://api.scaledown.xyz/compress/raw/
    """

    def __init__(self, config: Optional[ScaledownClientConfig] = None) -> None:
        if config is None:
            api_key = _env("SCALEDOWN_API_KEY")
            if not api_key:
                raise ScaledownClientError("Missing env var SCALEDOWN_API_KEY")
            config = ScaledownClientConfig(
                api_key=api_key,
                base_url=_env("SCALEDOWN_BASE_URL", "https://api.scaledown.xyz") or "https://api.scaledown.xyz",
                timeout_seconds=int(_env("SCALEDOWN_TIMEOUT_SECONDS", "30") or "30"),
            )
        self.config = config

    def compress(self, prompt: str) -> str:
        """
        Compress a prompt to reduce token count.

        Args:
            prompt: Full prompt text

        Returns:
            Compressed prompt text
        """
        url = f"{self.config.base_url.rstrip('/')}/compress/raw/"

        try:
            body = prompt.encode("utf-8")
            req = urllib.request.Request(
                url=url,
                data=body,
                method="POST",
                headers={
                    "x-api-key": self.config.api_key,
                    "Content-Type": "text/plain",
                    "Accept": "text/plain",
                },
            )

            with urllib.request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
                compressed = resp.read().decode("utf-8").strip()
                if not compressed:
                    raise ScaledownClientError("Empty response from ScaleDown compression")
                return compressed

        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")
            except Exception:
                pass
            raise ScaledownClientError(f"HTTP {e.code} from ScaleDown: {detail or e.reason}") from e
        except urllib.error.URLError as e:
            raise ScaledownClientError(f"Cannot connect to ScaleDown. Error: {e}") from e
        except Exception as e:
            raise ScaledownClientError(f"Error calling ScaleDown compression: {e}") from e

