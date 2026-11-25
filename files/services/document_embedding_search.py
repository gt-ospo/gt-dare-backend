"""Search each document embedding space with its matching query vector."""

from collections import defaultdict
from hashlib import sha256
from uuid import uuid4

from core.services.document_embedding_profiles import GRANITE
from core.services.vector_service import get_document_vector_service
from core.services.vector_integrity import VectorIntegrityError
from django.db import DatabaseError
from files.models import File
from files.services.spin_document_embedding_client import (
    SpinDocumentEmbeddingClient,
    SpinEmbeddingError,
)
from files.services.spin_embedding_connections import SpinEmbeddingConnectionUnavailable


class DocumentEmbeddingAccessError(ValueError):
    """The document owner's personal connection is not the requester's."""


def interleave_space_rankings(ranked_groups, top_k):
    """Fuse independent rankings by rank, never by incomparable vector scores."""
    if top_k <= 0:
        return []
    result = []
    for rank in range(max((len(group) for group in ranked_groups), default=0)):
        for group in ranked_groups:
            if rank < len(group):
                result.append(group[rank])
                if len(result) == top_k:
                    return result
    return result


def search_document_spaces(
    *,
    owner_id,
    actor_id,
    file_ids,
    query,
    query_text,
    granite_vector,
    top_k,
    operation_id,
    include_vector=False,
):
    """Return per-space ranked matches; callers must not compare vector scores."""
    files = list(
        File.active_objects.filter(user_id=owner_id, pk__in=file_ids)
        .select_related("user")
        .order_by("pk")
    )
    groups = defaultdict(list)
    for file in files:
        if file.embedding_model != GRANITE and file.spin_index_unavailable:
            raise DocumentEmbeddingAccessError(
                "This SPIN document's search index may be incomplete. "
                "Reprocess the document using Basic before searching it."
            )
        key = (
            file.embedding_model,
            file.embedding_connection_id,
            file.vector_db_source,
        )
        groups[key].append(file)
    if any(model != GRANITE for model, _, _ in groups):
        if not actor_id or actor_id != owner_id:
            raise DocumentEmbeddingAccessError(
                "SPIN documents can only be searched using the owner's own account."
            )
        if not operation_id:
            raise DocumentEmbeddingAccessError(
                "This search cannot safely record SPIN usage. Try a new conversation."
            )
        # Vector storage is external to the database. A failed deletion can
        # remove some vectors even if an enclosing database transaction rolls
        # back every status update. Verify the active generation on every
        # SPIN search instead of relying on a database flag alone.
        from core.services.index_health import STATE_VERIFIED, check_index_health

        for file in files:
            if (
                file.embedding_model != GRANITE
                and check_index_health(
                    file, allow_active_while_processing=True
                ).state != STATE_VERIFIED
            ):
                raise DocumentEmbeddingAccessError(
                    "This SPIN document's search index is unavailable or incomplete. "
                    "Reprocess the document using Basic before searching it."
                )
    ranked_groups = []
    # A single turn can legitimately invoke the same search more than once,
    # including through separate agent tools. Treat each invocation as a new
    # chargeable operation, while keeping batches inside it deduplicated.
    invocation_id = uuid4().hex
    for (model, connection_id, backend), group in groups.items():
        if model == GRANITE:
            vector = granite_vector
        else:
            # The request identity includes the query leg without storing its text.
            query_digest = sha256(query.encode()).hexdigest()
            identity = (
                f"{operation_id}:{invocation_id}:{model}:{connection_id}:{backend}:{query_digest}"
            )
            stable_id = "search:" + sha256(identity.encode()).hexdigest()
            client = SpinDocumentEmbeddingClient(
                user=group[0].user,
                connection_id=connection_id,
                model=model,
                operation_id=stable_id,
            )
            try:
                vector = client.create_embeddings(query)
            except SpinEmbeddingConnectionUnavailable as error:
                raise DocumentEmbeddingAccessError(str(error)) from error
            except (
                SpinEmbeddingError,
                VectorIntegrityError,
                DatabaseError,
            ) as error:
                raise DocumentEmbeddingAccessError(
                    "Could not search the SPIN document. Check your SPIN connection and try again."
                ) from error
        service = get_document_vector_service(owner_id, model, backend=backend)
        try:
            matches = service.search_documents(
                vector=vector,
                user_id=owner_id,
                file_ids=[f.pk for f in group],
                top_k=top_k,
                query_text=query_text,
                include_vector=include_vector and model == GRANITE,
            )
        finally:
            service.close()
        ranked_groups.append(matches)
    return ranked_groups
