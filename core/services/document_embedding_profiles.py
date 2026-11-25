"""Embedding spaces for personal document indexes.

The identifiers and collection suffixes are persistent index metadata. A new
embedding model must use a different suffix even if its dimensions match.
"""

from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class DocumentEmbeddingProfile:
    identifier: str
    label: str
    dimensions: int
    collection_suffix: str
    max_input_tokens: int


GRANITE = "granite"
SPIN_PROFILES = {
    "embeddinggemma-2": DocumentEmbeddingProfile(
        "embeddinggemma-2", "EmbeddingGemma 2", 768, "SpinEmbeddingGemma2", 8192
    ),
    "Nemotron-3.5-Embed-8B": DocumentEmbeddingProfile(
        "Nemotron-3.5-Embed-8B",
        "Nemotron 3.5 Embed 8B",
        4096,
        "SpinNemotron35Embed8B",
        32768,
    ),
}


def is_spin_base_url(url: str) -> bool:
    """Only the verified SPIN endpoint may use this personal-key integration."""
    try:
        parts = urlsplit(url)
        return (
            parts.scheme == "https"
            and parts.hostname == "spin.pace.gatech.edu"
            and parts.port == 4000
            and parts.username is None
            and parts.password is None
            and parts.path.rstrip("/") == "/v1"
            and not parts.query
            and not parts.fragment
        )
    except (ValueError, TypeError):
        return False
