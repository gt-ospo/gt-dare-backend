"""Resolve personal SPIN connections without exposing their credentials."""

from billing.constants import LiteLLMKeySourceChoice
from billing.models import LiteLLMKey
from core.services.document_embedding_profiles import is_spin_base_url


class SpinEmbeddingConnectionUnavailable(ValueError):
    """The connection is missing, expired, or is not the owner's SPIN key."""


def personal_spin_connections(user):
    return LiteLLMKey.visible_for_user(user).filter(
        source=LiteLLMKeySourceChoice.USER,
        owner_user=user,
    )


def resolve_personal_spin_connection(user, connection_id):
    connection = personal_spin_connections(user).filter(pk=connection_id).first()
    if connection is None or not is_spin_base_url(connection.base_url):
        raise SpinEmbeddingConnectionUnavailable(
            "This document's saved SPIN connection is unavailable. "
            "Upload the original file again using your current SPIN connection."
        )
    return connection
