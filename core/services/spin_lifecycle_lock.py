"""Serialize personal SPIN file creation with permanent account deletion."""

from hashlib import sha256

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models import F


class SpinLifecycleBusyError(RuntimeError):
    """An account deletion or SPIN upload is already in progress."""


def acquire_spin_lifecycle_lock(user_id):
    """Acquire a transaction-scoped per-user fence; caller owns transaction.

    PostgreSQL releases transaction advisory locks automatically on commit,
    rollback, or connection loss, including when a connection is pooled.
    SQLite has no advisory locks, so a no-op user-row write conservatively
    acquires its database write lock until the caller's transaction ends.
    """
    if not connection.in_atomic_block:
        raise RuntimeError("SPIN lifecycle fencing requires an atomic transaction")
    user_model = get_user_model()
    if connection.vendor == "postgresql":
        key = int.from_bytes(
            sha256(f"dare-spin-lifecycle:{user_id}".encode()).digest()[:8],
            byteorder="big",
            signed=True,
        )
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [key])
            if not cursor.fetchone()[0]:
                raise SpinLifecycleBusyError(
                    "A SPIN upload or account deletion is in progress. Please try again."
                )
        exists = user_model._base_manager.filter(pk=user_id).exists()
    elif connection.vendor == "sqlite":
        pk_field = user_model._meta.pk.attname
        exists = bool(
            user_model._base_manager.filter(pk=user_id).update(
                **{pk_field: F(pk_field)}
            )
        )
    else:
        raise SpinLifecycleBusyError(
            "SPIN document operations are unavailable for this database."
        )
    if not exists:
        raise SpinLifecycleBusyError("This account is no longer available.")
