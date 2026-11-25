"""Confirm personal SPIN indexes are removed before deleting their owner/file."""

from contextlib import contextmanager
from contextvars import ContextVar

from django.db.models import Q
from core.services.vector_service import get_document_vector_service
from core.services.document_embedding_profiles import GRANITE
from users.constants import VectorDBChoice


class SpinIndexDeletionError(RuntimeError):
    """A SPIN index could not be confirmed deleted; the file remains available."""

class SpinIndexBusyError(SpinIndexDeletionError):
    """Processing or an uncertain previous write makes deletion unsafe."""



_purged_accounts = ContextVar("spin_purged_accounts", default=frozenset())


@contextmanager
def spin_account_already_purged(user_id):
    token = _purged_accounts.set(_purged_accounts.get() | {user_id})
    try:
        yield
    finally:
        _purged_accounts.reset(token)


def assert_spin_file_quiescent(file):
    """Call while holding this File's row lock, before touching its vectors."""
    from files.models import VectorIndexAttempt

    uncertain = (
        VectorIndexAttempt.objects.filter(file_id=file.pk)
        .exclude(embedding_model=GRANITE)
        .filter(
            Q(status__in=("running", "abandoned"))
            | Q(status="failed", attempted_count__gt=0)
        )
        .exists()
    )
    if file.ingestion_token is not None:
        raise SpinIndexBusyError(
            "This document is still processing. Please wait for it to finish "
            "before deleting it."
        )
    if uncertain:
        raise SpinIndexBusyError(
            "A previous document-processing attempt may still have unfinished "
            "writes. Your file was kept; contact support for help deleting it."
        )


def purge_spin_file_generations(user_id, targets):
    if user_id in _purged_accounts.get():
        return
    for key, backend, model in targets:
        service = None
        try:
            service = get_document_vector_service(
                user_id, model,
                backend=backend if backend is not None else VectorDBChoice.WEAVIATE,
            )
            if service.delete_file_vectors(key, user_id) is not True:
                raise RuntimeError("Vector cleanup was not confirmed")
        except Exception as exc:
            raise SpinIndexDeletionError(
                "Could not delete the SPIN document index. "
                "Your original file was kept, but its search index may be unavailable. "
                "Please retry deletion."
            ) from exc
        finally:
            if service is not None:
                service.close()
