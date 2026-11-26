"""
Connection probe for LiteLLM proxy keys.

LiteLLM serves an OpenAI-compatible HTTP API at `<base>/v1/...`; the lightest
reachability + auth check is hitting `GET /v1/models`. An optional same-origin
metadata request identifies known non-chat modes. The gateway's `litellm_provider` names the upstream vendor, which says
nothing about how DARE reaches the model, and reading it as a transport was
a source of misrouting.
"""

from dataclasses import dataclass, field
from typing import List, Optional

import httpx


@dataclass(frozen=True)
class ProbedModel:
    name: str
    supports_chat: Optional[bool] = None


@dataclass
class LiteLLMProbeResult:
    ok: bool
    models: List[ProbedModel] = field(default_factory=list)
    error: str = ""

    @property
    def model_names(self) -> List[str]:
        return [m.name for m in self.models]

    @property
    def chat_model_names(self) -> List[str]:
        return [
            model.name for model in self.models
            if getattr(model, "supports_chat", None) is not False
        ]


def _chat_modes(root: str, headers: dict, timeout: float) -> dict:
    """Read optional LiteLLM metadata. Unknown modes retain legacy behavior."""
    try:
        response = httpx.get(
            root.removesuffix("/v1") + "/model/info",
            headers=headers,
            timeout=timeout,
            follow_redirects=False,
        )
        response.raise_for_status()
        entries = response.json().get("data", [])
        modes = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = entry.get("model_name")
            info = entry.get("model_info")
            if isinstance(name, str) and isinstance(info, dict):
                modes.setdefault(name, set()).add(info.get("mode"))
        non_chat = {
            "embedding", "image_generation", "audio_transcription",
            "audio_speech", "moderation", "rerank",
        }
        return {name: False for name, values in modes.items() if values and values <= non_chat}
    except (httpx.HTTPError, ValueError, AttributeError, TypeError):
        return {}


def probe_litellm_connection(
    base_url: str, api_key: str, *, timeout: float = 10.0
) -> LiteLLMProbeResult:
    # OpenAI-SDK convention: `base_url` is the API root (typically ending in
    # `/v1`), and endpoint paths are appended directly. We mirror that here
    # so the probe URL matches what the dispatcher will hit. If the caller
    # didn't include `/v1`, we add it once — never double-append.
    root = base_url.rstrip("/")
    if not root.rstrip("/").endswith("/v1"):
        root = root + "/v1"
    url = root + "/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        response = httpx.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        entries = payload.get("data", []) if isinstance(payload, dict) else []
        chat_modes = _chat_modes(root, headers, min(timeout, 3.0))
        models = [
            ProbedModel(
                name=str(entry.get("id")),
                supports_chat=chat_modes.get(str(entry.get("id"))),
            )
            for entry in entries
            if isinstance(entry, dict) and entry.get("id")
        ]
        return LiteLLMProbeResult(ok=True, models=models)
    except httpx.HTTPStatusError as e:
        return LiteLLMProbeResult(
            ok=False, error=f"HTTP {e.response.status_code}: {e.response.text[:200]}"
        )
    except Exception as e:
        return LiteLLMProbeResult(ok=False, error=f"{type(e).__name__}: {e}")
