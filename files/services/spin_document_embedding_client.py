"""Calls to a user's SPIN embedding service, with explicit usage recording."""

from openai import OpenAI, OpenAIError
from django.db import IntegrityError, transaction

from core.services.document_embedding_profiles import SPIN_PROFILES
from core.services.vector_integrity import VectorIntegrityError, validate_embedding
from files.services.spin_embedding_connections import resolve_personal_spin_connection
from files.models import SpinEmbeddingCall


class SpinEmbeddingError(RuntimeError):
    """A SPIN request failed without exposing provider or credential details."""


class SpinDocumentEmbeddingClient:
    def __init__(self, *, user, connection_id, model, operation_id, file_id=None):
        if model not in SPIN_PROFILES:
            raise ValueError("Unsupported SPIN embedding model")
        if not operation_id:
            raise ValueError("A stable operation identifier is required")
        self.user = user
        self.connection_id = connection_id
        self.profile = SPIN_PROFILES[model]
        self.operation_id = operation_id
        self.file_id = file_id
        self.batch_number = 0

    def create_embeddings(self, text):
        return self.create_batch_embeddings([text])[0]

    def create_batch_embeddings(self, texts):
        if not texts:
            return []
        # Resolve again on each request, including worker retries: a removed,
        # expired, or reassigned key must immediately stop being usable.
        connection = resolve_personal_spin_connection(self.user, self.connection_id)
        try:
            with transaction.atomic():
                call = SpinEmbeddingCall.objects.create(
                    user=self.user,
                    file_id=self.file_id,
                    operation_id=self.operation_id,
                    batch_number=self.batch_number,
                    model=self.profile.identifier,
                    connection_id=self.connection_id,
                )
        except IntegrityError:
            raise SpinEmbeddingError(
                "This SPIN request was already attempted. Check its status before trying again."
            ) from None
        try:
            with OpenAI(
                api_key=connection.api_key,
                base_url=connection.base_url,
                max_retries=0,
                timeout=60,
            ) as client:
                response = client.embeddings.create(
                    model=self.profile.identifier, input=texts
                )
        except OpenAIError:
            raise SpinEmbeddingError(
                "SPIN could not create document embeddings. Check your connection and SPIN account."
            ) from None
        # Persist success before interpreting the output. If this write fails,
        # the started row remains and a replay cannot silently spend again.
        usage = getattr(response, "usage", None)
        updated = SpinEmbeddingCall.objects.filter(pk=call.pk, state="started").update(
            state="complete", total_tokens=getattr(usage, "total_tokens", None)
        )
        if updated != 1:
            raise SpinEmbeddingError("Could not record the SPIN request result.")
        self.batch_number += 1
        if len(response.data) != len(texts):
            raise VectorIntegrityError("SPIN returned an incomplete embedding batch")
        ordered = sorted(response.data, key=lambda item: item.index)
        if [item.index for item in ordered] != list(range(len(texts))):
            raise VectorIntegrityError("SPIN returned invalid embedding indexes")
        for item in ordered:
            validate_embedding(item.embedding, self.profile.dimensions)
        return [item.embedding for item in ordered]
