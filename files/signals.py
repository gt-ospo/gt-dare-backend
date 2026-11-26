from functools import partial

from django.db import DatabaseError, transaction
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from core.services.document_embedding_profiles import GRANITE

from .models import File, VectorIndexAttempt
from .tasks import delete_file_vectors
from .services.spin_index_deletion import (
    SpinIndexBusyError,
    assert_spin_file_quiescent,
    purge_spin_file_generations,
)


@receiver(pre_delete, sender=File)
def delete_file_embeddings(sender, instance, **kwargs):
    """Snapshot index identities while deletion is serialized with publication."""
    using = kwargs["using"]
    try:
        current = (
            File._base_manager.using(using)
            .select_for_update(nowait=instance.embedding_model != GRANITE)
            .filter(pk=instance.pk)
            .first()
        )
    except DatabaseError as exc:
        if instance.embedding_model == GRANITE:
            raise
        raise SpinIndexBusyError(
            "This document is still processing. Please wait for it to finish before deleting it."
        ) from exc
    if current is None or not current.user_id:
        return
    attempts = list(
        VectorIndexAttempt.objects.using(using)
        .filter(file_id=current.pk)
        .values_list("generation", "backend", "embedding_model")
    )
    attempts.append(
        (current.vector_index_key, current.vector_db_source, current.embedding_model)
    )
    targets = [
        (key, backend) if model == GRANITE else (key, backend, model)
        for key, backend, model in attempts
    ]
    targets = list(dict.fromkeys(targets))
    # Retire older staged generations first. If cleanup stops partway
    # through, this gives the currently searchable generation the best
    # chance of remaining available until the final deletion.
    active_target = (
        (current.vector_index_key, current.vector_db_source)
        if current.embedding_model == GRANITE
        else (current.vector_index_key, current.vector_db_source, current.embedding_model)
    )
    if active_target in targets:
        targets.remove(active_target)
        targets.append(active_target)
    spin_targets = [target for target in targets if len(target) == 3]
    if spin_targets:
        assert_spin_file_quiescent(current)
        purge_spin_file_generations(current.user_id, spin_targets)
    granite_targets = [target for target in targets if len(target) == 2]
    if granite_targets:
        transaction.on_commit(
            partial(delete_file_vectors.delay, current.pk, current.user_id, granite_targets),
            using=using,
        )
