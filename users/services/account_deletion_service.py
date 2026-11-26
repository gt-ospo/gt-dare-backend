"""Permanent account deletion (expunge).

Erases a user's DARE identity and every trace of their data: database rows
(explicit sweeps plus FK cascade), the vector-store namespace, uploaded file
blobs, the avatar, email logs, and the linked SocraticBooks account.

Memory rows are not swept here — ``MemoryRecord``, ``MemoryLedgerEntry`` and
``UserMemoryDocument`` all cascade from the user, so the atomic DB delete owns
them.

External stores are purged best-effort: a failure there is recorded as a
warning and does not abort the expunge, because the account must not survive a
transient outage of a secondary store. The database portion is atomic.
"""

import logging
from dataclasses import dataclass, field

from django.contrib.auth import get_user_model
from django.db import DatabaseError, transaction
from django.db.models import Q
from django.db.models.deletion import ProtectedError

from agents.models import Agent, AgentNodeData, TemplateAgentNodeData
from billing.models import LiteLLMKey
from core.config.vector_db import get_user_namespace
from core.services.sb_client import SocraticBooksClient
from core.services.document_embedding_profiles import GRANITE, SPIN_PROFILES
from core.services.spin_lifecycle_lock import (
    SpinLifecycleBusyError,
    acquire_spin_lifecycle_lock,
)
from core.services.vector_service import get_document_vector_service, get_vector_service
from users.constants import VectorDBChoice
from email_logs.models import EmailLog
from files.models import File, SpinEmbeddingCall, VectorIndexAttempt
from files.services.spin_index_deletion import (
    SpinIndexBusyError,
    assert_spin_file_quiescent,
    spin_account_already_purged,
)
from users.services.avatar_service import AvatarService
from workflows.models import WorkflowNode

logger = logging.getLogger(__name__)


class AccountDeletionBlocked(Exception):
    """The account cannot be deleted until an administrative conflict is resolved."""

class SpinAccountPurgeBlocked(AccountDeletionBlocked):
    """The personal vector store did not confirm complete removal."""



@dataclass
class AccountDeletionReport:
    """What happened during an expunge, including non-fatal degradations."""

    user_id: int
    warnings: list[str] = field(default_factory=list)


class AccountDeletionService:
    """Orchestrate the permanent, unrecoverable deletion of a user account."""

    def expunge(self, user) -> AccountDeletionReport:
        user_id = user.id
        report = AccountDeletionReport(user_id=user_id)

        # Fail before touching any external store: keys this user administers
        # for other users are PROTECTed and would abort the DB delete midway.
        self._ensure_not_blocked(user)

        try:
            with transaction.atomic():
                # This fence blocks only SPIN uploads for this user. File
                # locks serialize against queued/acquiring workers and live
                # SPIN vector writes without blocking profile edits.
                try:
                    acquire_spin_lifecycle_lock(user_id)
                    spin_attempt_files = (
                        VectorIndexAttempt.objects.filter(owner_id=user_id)
                        .exclude(embedding_model=GRANITE)
                        .values_list("file_id", flat=True)
                    )
                    spin_files = list(
                        File._base_manager.filter(user_id=user_id)
                        .filter(
                            ~Q(embedding_model=GRANITE) | Q(pk__in=spin_attempt_files)
                        )
                        .order_by("pk")
                        .select_for_update(nowait=True)
                    )
                except (DatabaseError, SpinLifecycleBusyError) as exc:
                    raise AccountDeletionBlocked(
                        "Your account or a document is busy. Please try deleting the account again."
                    ) from exc
                for file in spin_files:
                    try:
                        assert_spin_file_quiescent(file)
                    except SpinIndexBusyError as exc:
                        raise AccountDeletionBlocked(str(exc)) from exc

                self._purge_spin_vector_namespaces_or_block(user_id)
                self._purge_vector_namespace(user_id, report)
                self._delete_file_blobs(user, report)
                self._delete_socratic_account(user_id, report)
                # AvatarService saves the User row. Do this after slow
                # external cleanup so concurrent profile updates are not
                # blocked for the duration of those network calls. Reload
                # under a lock because the avatar may have changed while the
                # external cleanup was in progress.
                current_user = get_user_model()._base_manager.select_for_update().get(
                    pk=user_id
                )
                self._remove_avatar(current_user, report)

                with spin_account_already_purged(user_id):
                    self._delete_database_records(current_user, current_user.email)
        except SpinAccountPurgeBlocked:
            # This update is outside the inner transaction so it survives that
            # rollback in ordinary requests. Search also checks vector health
            # in case an outer transaction rolls the marker back.
            self._mark_spin_indexes_unavailable(user_id)
            raise

        logger.info("Expunged account %s (%s): %s", user_id, current_user.email, report)
        return report

    def _delete_database_records(self, user, email) -> None:
            # PROTECT chains abort a cascade even when the protecting row is
            # itself part of it, so protected owners go first, in dependency
            # order: node data -> agents -> LiteLLM keys -> the user cascade.
            self._delete_workflow_node_data(user)
            Agent._base_manager.filter(user=user).delete()
            LiteLLMKey._base_manager.filter(
                Q(owner_user=user) | Q(assigned_user=user)
            ).delete()
            EmailLog._base_manager.filter(recipient__iexact=email).delete()
            try:
                user.delete()
            except ProtectedError as exc:
                raise AccountDeletionBlocked(
                    "Some records tied to this account are protected and could "
                    "not be removed. Nothing was deleted from the database."
                ) from exc

    def _ensure_not_blocked(self, user) -> None:
        administers_foreign_keys = (
            LiteLLMKey._base_manager.filter(created_by=user)
            .exclude(owner_user=user)
            .exclude(assigned_user=user)
            .exists()
        )
        if administers_foreign_keys:
            raise AccountDeletionBlocked(
                "This account administers LiteLLM keys that belong to other "
                "users. Reassign or remove those keys before deleting it."
            )

    def _purge_vector_namespace(
        self, user_id: int, report: AccountDeletionReport
    ) -> None:
        try:
            get_vector_service(user_id).delete_namespace(get_user_namespace(user_id))
        except Exception as exc:
            logger.warning("Vector purge failed for user %s: %s", user_id, exc)
            report.warnings.append("Vectorized data could not be reached for deletion.")
    def _purge_spin_vector_namespaces_or_block(self, user_id: int) -> None:
        # The durable call record outlives an individual File (its FK is set
        # to null), so a deleted file whose asynchronous cleanup failed still
        # causes the account namespace to be checked.
        has_spin_data = (
            SpinEmbeddingCall.objects.filter(user_id=user_id).exists()
            or File._base_manager.filter(user_id=user_id)
            .exclude(embedding_model="granite").exists()
            or VectorIndexAttempt.objects.filter(owner_id=user_id)
            .exclude(embedding_model="granite").exists()
        )
        if not has_spin_data:
            return
        for model in SPIN_PROFILES:
            service = None
            try:
                service = get_document_vector_service(user_id, model, backend=VectorDBChoice.WEAVIATE)
                if service.delete_namespace(get_user_namespace(user_id)) is not True:
                    raise RuntimeError("SPIN vector purge was not confirmed")
            except Exception as exc:
                logger.warning("SPIN vector purge failed for user %s", user_id)
                raise SpinAccountPurgeBlocked(
                    "Your SPIN document index could not be deleted. "
                    "Your account is still available, but some document searches "
                    "may be unavailable. Please retry deletion later."
                ) from exc
            finally:
                if service is not None:
                    service.close()

    @staticmethod
    def _mark_spin_indexes_unavailable(user_id):
        File._base_manager.filter(user_id=user_id).exclude(
            embedding_model=GRANITE
        ).update(
            spin_index_unavailable=True,
            error_message=(
                "Account deletion did not finish. This file was kept, "
                "but its search index may be incomplete. Retry account "
                "deletion or reprocess using Basic to restore search."
            ),
        )

    def _delete_file_blobs(self, user, report: AccountDeletionReport) -> None:
        """Remove stored file contents; row deletion alone leaves blobs behind."""
        failed = 0
        for stored_file in File._base_manager.filter(user=user).iterator():
            try:
                if stored_file.file:
                    stored_file.file.delete(save=False)
            except Exception as exc:
                failed += 1
                logger.warning(
                    "Blob delete failed for file %s (user %s): %s",
                    stored_file.id,
                    user.id,
                    exc,
                )
        if failed:
            report.warnings.append(
                f"{failed} uploaded file(s) could not be removed from storage."
            )

    def _remove_avatar(self, user, report: AccountDeletionReport) -> None:
        try:
            AvatarService.remove_avatar(user)
        except Exception as exc:
            logger.warning("Avatar removal failed for user %s: %s", user.id, exc)
            report.warnings.append("Avatar image could not be removed from storage.")

    def _delete_socratic_account(
        self, user_id: int, report: AccountDeletionReport
    ) -> None:
        deleted = SocraticBooksClient.delete_user(user_id)
        if not deleted:
            report.warnings.append(
                "The linked SocraticBooks account could not be deleted."
            )

    def _delete_workflow_node_data(self, user) -> None:
        """Delete node-data rows before the FK cascade runs.

        WorkflowNode reaches its data through a GenericForeignKey, so the
        cascade removes nodes but strands their data rows — and stranded
        Agent/TemplateAgent node data PROTECTs the user's agents and prompts,
        which would abort ``user.delete()``.
        """
        nodes = WorkflowNode._base_manager.filter(workflow__user=user).select_related(
            "data_content_type"
        )
        for node in nodes.iterator():
            data_object = node.data_object
            if data_object is not None:
                data_object.delete()

        AgentNodeData._base_manager.filter(
            Q(agent__user=user) | Q(prompt__user=user)
        ).delete()
        TemplateAgentNodeData._base_manager.filter(agent__user=user).delete()
