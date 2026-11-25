"""Isolated native SPIN/Granite ingestion and Weaviate integration checks.

SPIN and Granite embedding *providers* are simulated; Django, PostgreSQL,
document processing, access checks, and the vector server are real.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import json
import httpx
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event
import time
from uuid import uuid4
from openai import OpenAI as RealOpenAI

from django.contrib.auth import get_user_model
from django.conf import settings
from asgiref.sync import async_to_sync
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections, connection as db_connection, transaction
from django.utils import timezone
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from billing.constants import LiteLLMKeySourceChoice
from billing.models import LiteLLMKey
from core.helpers.openai import OpenAIWrapper
from core.services.document_ingestion_service import DocumentIngestionCommand, DocumentIngestionService
from core.services.document_processor import DocumentProcessor
from core.services.file_upload_service import FileUploadService
from core.services.rag.dtos import RetrievalRequest
from core.services.rag.retriever import DocumentRetriever
from core.services.spin_lifecycle_lock import acquire_spin_lifecycle_lock
from core.services.vector_service import get_document_vector_service, WeaviateVectorService
from files.api.views import FileViewSet
from files.constants import FileStatus
from files.models import File, SpinEmbeddingCall, VectorIndexAttempt
from files.services.document_embedding_search import DocumentEmbeddingAccessError, search_document_spaces
from files.services.document_reprocessing_service import DocumentReprocessingCommand, DocumentReprocessingService, ReprocessingUnavailable
from files.services.spin_index_deletion import SpinIndexDeletionError
from users.services.account_deletion_service import AccountDeletionBlocked, AccountDeletionService
from users.services.avatar_service import AvatarService
from workflows.handlers.base import NodeExecutionContext
from workflows.handlers.step_handler import StepNodeHandler
from workflows.models import StepNodeData, Workflow, WorkflowRun
from users.constants import VectorDBChoice


def fake_provider(request):
    assert str(request.url) == "https://spin.pace.gatech.edu:4000/v1/embeddings"
    assert request.headers["Authorization"] in {
        "Bearer isolated-test-key", "Bearer replacement-isolated-key"
    }
    payload = json.loads(request.content)
    model = payload["model"]
    dimensions = {"embeddinggemma-2": 768, "Nemotron-3.5-Embed-8B": 4096}[model]
    return httpx.Response(200, json={
        "object": "list", "model": model,
        "data": [{"object": "embedding", "index": i, "embedding": [0.1] * dimensions} for i in range(len(payload["input"]))],
        "usage": {"prompt_tokens": 9, "total_tokens": 9},
    })


def FakeOpenAI(**kwargs):
    # The real OpenAI client constructs and parses requests. Only transport is simulated.
    return RealOpenAI(http_client=httpx.Client(transport=httpx.MockTransport(fake_provider)), **kwargs)


@override_settings(SPIN_DOCUMENT_EMBEDDINGS_ENABLED=True)
class RealVectorEmbeddingIntegration(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="spin-native@example.invalid", password="pw", vector_db=VectorDBChoice.WEAVIATE)
        self.other = get_user_model().objects.create_user(email="spin-other@example.invalid", password="pw", vector_db=VectorDBChoice.WEAVIATE)
        self.connection = LiteLLMKey.objects.create(label="Test SPIN", base_url="https://spin.pace.gatech.edu:4000/v1", api_key="isolated-test-key", source=LiteLLMKeySourceChoice.USER, owner_user=self.user, created_by=self.user)
        self.factory = APIRequestFactory()

    def upload(self, model):
        payload = {"files": [SimpleUploadedFile(f"{model}.txt", b"A workshop happens on Tuesday at the library. Participants will learn to search documents.\n" * 5, content_type="text/plain")], "names": [f"{model}.txt"], "processing_mode": "basic"}
        if model != "granite":
            payload.update(embedding_model=model, embedding_connection_id=str(self.connection.pk))
        request = self.factory.post("/files/api/files/", payload, format="multipart")
        force_authenticate(request, user=self.user)
        with patch("core.services.file_upload_service.enqueue", side_effect=lambda *args, **kwargs: SimpleNamespace(id=kwargs.get("job_id", "granite-job"))):
            response = FileViewSet.as_view({"post": "create"})(request)
        self.assertEqual(response.status_code, 201, response.data)
        return File.active_objects.get(pk=response.data[0]["id"])

    def test_concurrent_worker_cannot_acquire_deleted_file(self):
        # A worker trying to acquire a lease must wait for the transaction
        # that is deleting the file, then observe that the row is gone.
        from files.services.spin_index_deletion import purge_spin_file_generations

        with patch("files.services.spin_document_embedding_client.OpenAI", FakeOpenAI), patch("core.services.rag.entity_extractor.NerExtractor._get_predictor", return_value=lambda text, labels, threshold: []):
            file = self.upload("embeddinggemma-2")
            DocumentIngestionService().process(DocumentIngestionCommand.from_raw(file.pk))
            file.refresh_from_db()
            self.assertEqual(file.status, FileStatus.PROCESSED)

        deletion_locked = Event()
        allow_deletion = Event()
        acquisition_started = Event()
        worker_pid = Queue()

        def hold_lock(*args, **kwargs):
            deletion_locked.set()
            if not allow_deletion.wait(15):
                raise RuntimeError("Timed out waiting to release test deletion lock")
            return purge_spin_file_generations(*args, **kwargs)

        def delete_file():
            close_old_connections()
            try:
                request = self.factory.delete(f"/files/api/files/{file.pk}/")
                force_authenticate(request, user=self.user)
                return FileViewSet.as_view({"delete": "destroy"})(request, pk=file.pk).status_code
            finally:
                close_old_connections()

        def acquire_lease():
            close_old_connections()
            try:
                with db_connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    worker_pid.put(cursor.fetchone()[0])
                acquisition_started.set()
                return File.active_objects.filter(pk=file.pk, ingestion_token__isnull=True).update(ingestion_token=uuid4(), ingestion_started_at=timezone.now())
            finally:
                close_old_connections()

        with patch("files.signals.purge_spin_file_generations", side_effect=hold_lock), ThreadPoolExecutor(max_workers=2) as executor:
            deleting = executor.submit(delete_file)
            try:
                self.assertTrue(deletion_locked.wait(15))
                acquiring = executor.submit(acquire_lease)
                self.assertTrue(acquisition_started.wait(15))
                pid = worker_pid.get(timeout=5)
                deadline = time.monotonic() + 5
                blocked = False
                while time.monotonic() < deadline:
                    with db_connection.cursor() as cursor:
                        cursor.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", [pid])
                        row = cursor.fetchone()
                    if row and row[0] == "Lock":
                        blocked = True
                        break
                    if acquiring.done():
                        break
                    time.sleep(0.05)
                self.assertTrue(blocked, "the worker did not wait for the deletion row lock")
            finally:
                allow_deletion.set()
            self.assertEqual(deleting.result(timeout=20), 204)
            self.assertEqual(acquiring.result(timeout=20), 0)

    def test_active_or_uncertain_ingestion_blocks_deletion(self):
        with patch("files.services.spin_document_embedding_client.OpenAI", FakeOpenAI), patch("core.services.rag.entity_extractor.NerExtractor._get_predictor", return_value=lambda text, labels, threshold: []):
            file = self.upload("embeddinggemma-2")
            DocumentIngestionService().process(DocumentIngestionCommand.from_raw(file.pk))
            file.refresh_from_db()
            self.assertEqual(file.status, FileStatus.PROCESSED)

        def attempt_delete():
            request = self.factory.delete(f"/files/api/files/{file.pk}/")
            force_authenticate(request, user=self.user)
            return FileViewSet.as_view({"delete": "destroy"})(request, pk=file.pk)

        def assert_original_present():
            self.assertTrue(File.active_objects.filter(pk=file.pk).exists())
            service = get_document_vector_service(self.user.pk, file.embedding_model, backend=VectorDBChoice.WEAVIATE)
            try:
                self.assertTrue(service.read_generation(file.vector_index_key, self.user.pk, file.pk))
            finally:
                service.close()

        # A live ingestion lease blocks both operations without deleting data.
        File.active_objects.filter(pk=file.pk).update(ingestion_token=uuid4(), ingestion_started_at=timezone.now(), status=FileStatus.PROCESSING)
        self.assertEqual(attempt_delete().status_code, 503)
        with self.assertRaises(AccountDeletionBlocked):
            AccountDeletionService().expunge(self.user)
        assert_original_present()

        # A crashed/superseded writer can have lost its lease while leaving an
        # uncertain external write. The deletion remains blocked until an
        # operator reconciles it; we model that reconciliation in test only.
        File.active_objects.filter(pk=file.pk).update(ingestion_token=None, ingestion_started_at=None, status=FileStatus.PROCESSED)
        attempt = VectorIndexAttempt.objects.create(file=file, generation=uuid4().hex, owner_id=self.user.pk, backend=VectorDBChoice.WEAVIATE, embedding_model=file.embedding_model, status="abandoned", attempted_count=1)
        def crash_worker_connection():
            close_old_connections()
            db_connection.set_autocommit(False)
            with db_connection.cursor() as cursor:
                cursor.execute("SELECT id FROM files_file WHERE id = %s FOR UPDATE", [file.pk])
                self.assertIsNotNone(cursor.fetchone())
            # Closing the connection without committing simulates a dead
            # worker; PostgreSQL must release the exclusive row lock.
            db_connection.close()
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(crash_worker_connection).result(timeout=15)
        with transaction.atomic():
            self.assertEqual(File.active_objects.select_for_update(nowait=True).get(pk=file.pk).pk, file.pk)
        self.assertEqual(attempt_delete().status_code, 503)
        with self.assertRaises(AccountDeletionBlocked):
            AccountDeletionService().expunge(self.user)
        assert_original_present()
        attempt.status = "failed"
        attempt.save(update_fields=["status"])
        self.assertEqual(attempt_delete().status_code, 503)
        assert_original_present()
        attempt.status = "retired"
        attempt.save(update_fields=["status"])
        self.assertEqual(attempt_delete().status_code, 204)

    def test_active_vector_write_blocks_file_and_account_deletion(self):
        # Exercise the real worker write path with a delayed Weaviate upsert,
        # including a superseded attempt that is still inside its write.
        writing = Event()
        release_write = Event()
        original = WeaviateVectorService.upsert_vectors

        def delayed_write(service, *args, **kwargs):
            writing.set()
            if not release_write.wait(15):
                raise RuntimeError("Timed out waiting to release test write")
            return original(service, *args, **kwargs)

        def worker(file_id):
            close_old_connections()
            try:
                return DocumentIngestionService().process(DocumentIngestionCommand.from_raw(file_id))
            finally:
                close_old_connections()

        with patch("files.services.spin_document_embedding_client.OpenAI", FakeOpenAI), patch("core.services.rag.entity_extractor.NerExtractor._get_predictor", return_value=lambda text, labels, threshold: []), patch.object(WeaviateVectorService, "upsert_vectors", delayed_write):
            file = self.upload("embeddinggemma-2")
            with ThreadPoolExecutor(max_workers=1) as executor:
                processing = executor.submit(worker, file.pk)
                try:
                    self.assertTrue(writing.wait(15))
                    # A coordinator can mark an attempt abandoned even while
                    # its original writer is still executing.
                    self.assertEqual(VectorIndexAttempt.objects.filter(file=file, status="running").update(status="abandoned"), 1)
                    request = self.factory.delete(f"/files/api/files/{file.pk}/")
                    force_authenticate(request, user=self.user)
                    self.assertEqual(FileViewSet.as_view({"delete": "destroy"})(request, pk=file.pk).status_code, 503)
                    with self.assertRaises(AccountDeletionBlocked):
                        AccountDeletionService().expunge(self.user)
                    self.assertTrue(File.active_objects.filter(pk=file.pk).exists())
                finally:
                    release_write.set()
                processing.result(timeout=30)
            file.refresh_from_db()
            self.assertEqual(file.status, FileStatus.PROCESSED, file.error_message)
            request = self.factory.delete(f"/files/api/files/{file.pk}/")
            force_authenticate(request, user=self.user)
            self.assertEqual(FileViewSet.as_view({"delete": "destroy"})(request, pk=file.pk).status_code, 204)

    def test_granite_only_account_deletion_keeps_existing_path(self):
        with patch.object(OpenAIWrapper, "create_batch_embeddings", side_effect=lambda texts: [[0.1] * 3072 for _ in texts]), patch("core.services.rag.entity_extractor.NerExtractor._get_predictor", return_value=lambda text, labels, threshold: []):
            file = self.upload("granite")
            DocumentIngestionService().process(DocumentIngestionCommand.from_raw(file.pk))
        user_id, file_id, generation = self.user.pk, file.pk, file.vector_index_key
        file.refresh_from_db()
        generation = file.vector_index_key
        with patch("users.services.account_deletion_service.get_document_vector_service", side_effect=AssertionError("Granite-only deletion must not touch SPIN")), patch("users.services.account_deletion_service.SocraticBooksClient.delete_user", return_value=True), patch("files.signals.delete_file_vectors.delay", return_value=None):
            report = AccountDeletionService().expunge(self.user)
        self.assertEqual(report.warnings, [])
        self.assertFalse(get_user_model().objects.filter(pk=user_id).exists())
        service = get_document_vector_service(user_id, "granite", backend=VectorDBChoice.WEAVIATE)
        try:
            self.assertEqual(service.read_generation(generation, user_id, file_id), [])
        finally:
            service.close()

    def test_account_with_foreign_protected_key_is_not_touched(self):
        other_key = LiteLLMKey.objects.create(label="Assigned to another user", base_url="https://spin.pace.gatech.edu:4000/v1", api_key="synthetic-only", source=LiteLLMKeySourceChoice.ADMIN_USER, assigned_user=self.other, created_by=self.user)
        with patch.object(AccountDeletionService, "_purge_vector_namespace") as purge:
            with self.assertRaises(AccountDeletionBlocked):
                AccountDeletionService().expunge(self.user)
            purge.assert_not_called()
        self.assertTrue(get_user_model().objects.filter(pk=self.user.pk).exists())
        self.assertTrue(LiteLLMKey.objects.filter(pk=other_key.pk).exists())
        self.assertTrue(LiteLLMKey.objects.filter(pk=self.connection.pk).exists())

    def test_spin_purge_failure_keeps_account_file_and_connection(self):
        with patch("files.services.spin_document_embedding_client.OpenAI", FakeOpenAI), patch("core.services.rag.entity_extractor.NerExtractor._get_predictor", return_value=lambda text, labels, threshold: []):
            file = self.upload("embeddinggemma-2")
            DocumentIngestionService().process(DocumentIngestionCommand.from_raw(file.pk))
        partial_ack = SimpleNamespace(failed=1, matches=3, successful=2)
        with patch("weaviate.collections.data.executor._DataCollectionExecutor.delete_many", return_value=partial_ack), self.assertRaises(AccountDeletionBlocked):
            AccountDeletionService().expunge(self.user)
        self.assertTrue(get_user_model().objects.filter(pk=self.user.pk).exists())
        self.assertTrue(File.active_objects.filter(pk=file.pk).exists())
        self.assertTrue(LiteLLMKey.objects.filter(pk=self.connection.pk).exists())
        file.refresh_from_db()
        self.assertTrue(file.file.storage.exists(file.file.name))

    def test_upload_cannot_race_account_deletion(self):
        user_id = self.user.pk
        deletion_locked, allow_deletion = Event(), Event()
        original = AccountDeletionService._purge_vector_namespace

        def hold_lock(service, *args):
            deletion_locked.set()
            if not allow_deletion.wait(15):
                raise RuntimeError("Timed out waiting to release test account lock")
            return original(service, *args)

        def delete_account():
            close_old_connections()
            try:
                return AccountDeletionService().expunge(self.user)
            finally:
                close_old_connections()

        def upload_during_delete():
            close_old_connections()
            try:
                payload = {"files": [SimpleUploadedFile("race.txt", b"Small test document.", content_type="text/plain")], "names": ["race.txt"], "processing_mode": "basic", "embedding_model": "embeddinggemma-2", "embedding_connection_id": str(self.connection.pk)}
                request = self.factory.post("/files/api/files/", payload, format="multipart")
                force_authenticate(request, user=self.user)
                return FileViewSet.as_view({"post": "create"})(request).status_code
            finally:
                close_old_connections()

        with patch.object(AccountDeletionService, "_purge_vector_namespace", hold_lock), patch("users.services.account_deletion_service.SocraticBooksClient.delete_user", return_value=True), ThreadPoolExecutor(max_workers=2) as executor:
            deleting = executor.submit(delete_account)
            try:
                self.assertTrue(deletion_locked.wait(15))
                uploading = executor.submit(upload_during_delete)
                # A concurrent SPIN upload is refused promptly while the
                # account-deletion fence is held.
                self.assertGreaterEqual(uploading.result(timeout=5), 400)
            finally:
                allow_deletion.set()
            deleting.result(timeout=20)
        self.assertFalse(get_user_model().objects.filter(pk=user_id).exists())
        self.assertFalse(File._base_manager.filter(user_id=user_id).exists())

    def test_account_deletion_cannot_race_first_spin_upload(self):
        entered_upload, release_upload = Event(), Event()
        original = FileUploadService.create_file_record

        def hold_upload(*args, **kwargs):
            entered_upload.set()
            if not release_upload.wait(15):
                raise RuntimeError("Timed out waiting to release upload")
            return original(*args, **kwargs)

        def start_upload():
            close_old_connections()
            try:
                return self.upload("embeddinggemma-2").pk
            finally:
                close_old_connections()

        with patch.object(FileUploadService, "create_file_record", side_effect=hold_upload), ThreadPoolExecutor(max_workers=1) as executor:
            uploading = executor.submit(start_upload)
            try:
                self.assertTrue(entered_upload.wait(15))
                with self.assertRaises(AccountDeletionBlocked):
                    AccountDeletionService().expunge(self.user)
                self.assertTrue(get_user_model().objects.filter(pk=self.user.pk).exists())
            finally:
                release_upload.set()
            file_id = uploading.result(timeout=20)
        self.assertTrue(File.active_objects.filter(pk=file_id).exists())
        # The queued job has no worker lease and may be deleted safely.
        with patch("users.services.account_deletion_service.SocraticBooksClient.delete_user", return_value=True), patch("files.signals.delete_file_vectors.delay", return_value=None):
            AccountDeletionService().expunge(self.user)
        self.assertFalse(File.active_objects.filter(pk=file_id).exists())

    def test_spin_lifecycle_fence_releases_on_rollback_and_disconnect(self):
        user_id = self.user.pk
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with transaction.atomic():
                acquire_spin_lifecycle_lock(user_id)
                raise RuntimeError("rollback")
        with transaction.atomic():
            acquire_spin_lifecycle_lock(user_id)
        acquired_before_crash = Event()
        def close_lock_connection():
            close_old_connections()
            db_connection.set_autocommit(False)
            # The lock helper requires Django's atomic context, so enter one
            # and close the connection without committing to emulate a crash.
            with transaction.atomic():
                acquire_spin_lifecycle_lock(user_id)
                acquired_before_crash.set()
                db_connection.close()
        with ThreadPoolExecutor(max_workers=1) as executor:
            try:
                executor.submit(close_lock_connection).result(timeout=15)
            except Exception:
                pass  # a closed connection may report rollback failure
        self.assertTrue(acquired_before_crash.is_set())
        with transaction.atomic():
            acquire_spin_lifecycle_lock(user_id)

    def test_old_backend_inserts_use_database_defaults(self):
        # Simulate pre-migration INSERT statements that know nothing about the
        # three new non-null columns. Real old-image compatibility is checked
        # separately by the release-image test.
        file = File.active_objects.create(user=self.user, name="old-client.txt", file="old-client.txt", status=FileStatus.PROCESSED, vector_db_source=VectorDBChoice.WEAVIATE)
        attempt = VectorIndexAttempt.objects.create(file=file, generation=uuid4().hex, owner_id=self.user.pk, backend=VectorDBChoice.WEAVIATE, status="published")
        quote = db_connection.ops.quote_name
        file_columns = [f.column for f in File._meta.local_concrete_fields if not f.primary_key and f.name not in {"embedding_model", "spin_index_unavailable"}]
        columns = ", ".join(map(quote, file_columns))
        with db_connection.cursor() as cursor:
            cursor.execute(f"INSERT INTO {quote(File._meta.db_table)} ({columns}) SELECT {columns} FROM {quote(File._meta.db_table)} WHERE id = %s RETURNING embedding_model, spin_index_unavailable", [file.pk])
            self.assertEqual(cursor.fetchone(), ("granite", False))
            attempt_columns = [f.column for f in VectorIndexAttempt._meta.local_concrete_fields if not f.primary_key and f.name != "embedding_model"]
            columns = ", ".join(map(quote, attempt_columns))
            values = ", ".join("%s" if c == "generation" else quote(c) for c in attempt_columns)
            cursor.execute(f"INSERT INTO {quote(VectorIndexAttempt._meta.db_table)} ({columns}) SELECT {values} FROM {quote(VectorIndexAttempt._meta.db_table)} WHERE id = %s RETURNING embedding_model", [uuid4().hex, attempt.pk])
            self.assertEqual(cursor.fetchone(), ("granite",))
    def test_slow_granite_account_deletion_does_not_lock_other_users(self):
        user_id, other_id = self.user.pk, self.other.pk
        cleanup_started, release_cleanup = Event(), Event()

        def slow_cleanup(service, *args):
            cleanup_started.set()
            if not release_cleanup.wait(15):
                raise RuntimeError("Timed out waiting to release cleanup")

        def delete_account():
            close_old_connections()
            try:
                return AccountDeletionService().expunge(self.user)
            finally:
                close_old_connections()

        def update_deleting_user():
            close_old_connections()
            try:
                return get_user_model().objects.filter(pk=user_id).update(first_name="After deletion")
            finally:
                close_old_connections()

        with patch.object(AccountDeletionService, "_delete_socratic_account", slow_cleanup), ThreadPoolExecutor(max_workers=2) as executor:
            deleting = executor.submit(delete_account)
            try:
                self.assertTrue(cleanup_started.wait(15))
                # A different user's ordinary account update must proceed.
                self.assertEqual(get_user_model().objects.filter(pk=other_id).update(first_name="Unaffected"), 1)
                updating = executor.submit(update_deleting_user)
                # A same-user profile update should not wait for slow cleanup.
                self.assertEqual(updating.result(timeout=5), 1)
            finally:
                release_cleanup.set()
            deleting.result(timeout=20)
        self.assertFalse(get_user_model().objects.filter(pk=user_id).exists())
        self.assertTrue(get_user_model().objects.filter(pk=other_id, first_name="Unaffected").exists())

    def test_avatar_changed_during_slow_cleanup_is_removed(self):
        user_id = self.user.pk
        cleanup_started, release_cleanup = Event(), Event()
        def slow_cleanup(service, *args):
            cleanup_started.set()
            if not release_cleanup.wait(15):
                raise RuntimeError("Timed out waiting to release cleanup")
        def delete_account():
            close_old_connections()
            try:
                return AccountDeletionService().expunge(self.user)
            finally:
                close_old_connections()
        with patch.object(AccountDeletionService, "_delete_socratic_account", slow_cleanup), ThreadPoolExecutor(max_workers=1) as executor:
            deleting = executor.submit(delete_account)
            try:
                self.assertTrue(cleanup_started.wait(15))
                refreshed = get_user_model().objects.get(pk=user_id)
                avatar = SimpleUploadedFile("avatar.png", b"synthetic-image-content", content_type="image/png")
                request = SimpleNamespace(build_absolute_uri=lambda path: f"https://test.example{path}")
                AvatarService.upload_avatar(refreshed, avatar, request)
                path = refreshed.avatar_url.replace(settings.MEDIA_URL, "", 1)
                self.assertTrue(default_storage.exists(path))
            finally:
                release_cleanup.set()
            deleting.result(timeout=20)
        self.assertFalse(get_user_model().objects.filter(pk=user_id).exists())
        self.assertFalse(default_storage.exists(path))

    def test_upload_index_mixed_search_and_delete(self):
        files = []
        with patch("files.services.spin_document_embedding_client.OpenAI", FakeOpenAI), patch.object(OpenAIWrapper, "create_batch_embeddings", side_effect=lambda texts: [[0.1] * 3072 for _ in texts]), patch("core.services.rag.entity_extractor.NerExtractor._get_predictor", return_value=lambda text, labels, threshold: []):
            for model in ("granite", "embeddinggemma-2", "Nemotron-3.5-Embed-8B"):
                file = self.upload(model)
                DocumentIngestionService().process(DocumentIngestionCommand.from_raw(file.pk))
                file.refresh_from_db()
                self.assertEqual(file.status, FileStatus.PROCESSED, file.error_message)
                self.assertEqual(file.embedding_model, model)
                self.assertTrue(file.index_generation)
                files.append(file)

            groups = search_document_spaces(owner_id=self.user.pk, actor_id=self.user.pk, file_ids=[f.pk for f in files], query="Where is the workshop?", query_text="workshop", granite_vector=[0.1] * 3072, top_k=5, operation_id="native-test-query")
            self.assertEqual(len(groups), 3)
            found = {m["metadata"]["file_id"] for group in groups for m in group}
            self.assertEqual(found, {str(f.pk) for f in files})
            self.assertEqual(SpinEmbeddingCall.objects.filter(state="complete").count(), 4)
            # Identical tool calls in one turn are legitimate distinct operations.
            again = search_document_spaces(owner_id=self.user.pk, actor_id=self.user.pk, file_ids=[f.pk for f in files], query="Where is the workshop?", query_text="workshop", granite_vector=[0.1] * 3072, top_k=5, operation_id="native-test-query")
            self.assertEqual({m["metadata"]["file_id"] for g in again for m in g}, found)
            self.assertEqual(SpinEmbeddingCall.objects.filter(state="complete").count(), 6)
            with self.assertRaises(DocumentEmbeddingAccessError):
                search_document_spaces(owner_id=self.user.pk, actor_id=self.other.pk, file_ids=[f.pk for f in files], query="workshop", query_text="", granite_vector=[0.1] * 3072, top_k=5, operation_id="unauthorized")
            # Exercise the actual standard-chat and advanced RAG retriever entry points.
            standard = DocumentProcessor()._query_document_vectors(user_id=self.user.pk, actor_user_id=self.user.pk, operation_id="standard-turn", vector=[0.1] * 3072, file_ids=[f.pk for f in files], top_k=5, query_text="workshop")
            self.assertEqual({m["metadata"]["file_id"] for m in standard}, found)
            request = RetrievalRequest(query="workshop", file_ids=tuple(f.pk for f in files), user_id=self.user.pk, payer_user_id=self.user.pk, top_k=5, embedding_operation_id="advanced-turn")
            advanced = DocumentRetriever().search(request, [0.1] * 3072, "workshop", want_vectors=True)
            self.assertEqual({c.file_id for c in advanced}, found)
            foreign_request = RetrievalRequest(query="workshop", file_ids=tuple(f.pk for f in files), user_id=self.user.pk, payer_user_id=self.other.pk, top_k=5, embedding_operation_id="foreign-workflow")
            prior_calls = SpinEmbeddingCall.objects.count()
            with self.assertRaises(DocumentEmbeddingAccessError):
                DocumentRetriever().search(foreign_request, [0.1] * 3072, "workshop", want_vectors=True)
            self.assertEqual(SpinEmbeddingCall.objects.count(), prior_calls)

            # Run the real shared-workflow step dispatch up to the security
            # boundary with actual workflow, step, and file relations in the DB.
            workflow = Workflow.objects.create(user=self.user, is_published=True)
            step = StepNodeData.objects.create()
            step.embedding_files.add(files[1])
            run = WorkflowRun.objects.create(workflow=workflow, user=self.other)
            context = NodeExecutionContext(workflow_run=run, previous_results={})
            handler = StepNodeHandler()
            prior_calls = SpinEmbeddingCall.objects.count()
            with patch.object(handler, "_get_llm_for_step", new_callable=AsyncMock), self.assertRaisesRegex(ValueError, "personal SPIN connection"):
                async_to_sync(handler._call_llm)(step, "workshop", context, None, None, "test-node")
            self.assertEqual(SpinEmbeddingCall.objects.count(), prior_calls)
            step.embedding_files.set([files[0]])
            config = async_to_sync(handler._get_step_execution_config)(step, context)
            async_to_sync(handler._check_spin_source_access)(config, context)

            spin_file = files[1]
            old_generation = spin_file.index_generation
            command = DocumentReprocessingCommand(file_id=spin_file.pk, user_id=self.user.pk, action="reparse", processing_mode="advanced")
            with self.assertRaises(ReprocessingUnavailable):
                DocumentReprocessingService().start(command)
            spin_file.refresh_from_db()
            self.assertEqual(spin_file.index_generation, old_generation)
            self.assertEqual(spin_file.status, FileStatus.PROCESSED)

            command = DocumentReprocessingCommand(file_id=spin_file.pk, user_id=self.user.pk, action="reparse", processing_mode="basic")
            with patch("files.services.document_reprocessing_service.enqueue"):
                queued = DocumentReprocessingService().start(command)
            self.assertTrue(search_document_spaces(owner_id=self.user.pk, actor_id=self.user.pk, file_ids=[spin_file.pk], query="workshop", query_text="workshop", granite_vector=[0.1] * 3072, top_k=5, operation_id="while-processing")[0])
            DocumentIngestionService().process(DocumentIngestionCommand(file_id=spin_file.pk, expected_job_id=queued.job_id, reprocessing_action="reparse", processing_mode="basic", previous_status=FileStatus.PROCESSED))
            spin_file.refresh_from_db()
            self.assertEqual(spin_file.status, FileStatus.PROCESSED, spin_file.error_message)
            self.assertNotEqual(spin_file.index_generation, old_generation)
            self.assertEqual(spin_file.embedding_model, "embeddinggemma-2")

            # A failed replacement keeps the published generation searchable.
            active_generation = spin_file.index_generation
            with patch("files.services.document_reprocessing_service.enqueue"):
                queued = DocumentReprocessingService().start(command)
            with patch("files.services.spin_document_embedding_client.OpenAI", side_effect=RuntimeError("simulated provider failure")), self.assertRaises(RuntimeError):
                DocumentIngestionService().process(DocumentIngestionCommand(file_id=spin_file.pk, expected_job_id=queued.job_id, reprocessing_action="reparse", processing_mode="basic", previous_status=FileStatus.PROCESSED))
            spin_file.refresh_from_db()
            self.assertEqual(spin_file.index_generation, active_generation)
            self.assertEqual(spin_file.status, FileStatus.PROCESSED)
            self.assertTrue(search_document_spaces(owner_id=self.user.pk, actor_id=self.user.pk, file_ids=[spin_file.pk], query="workshop", query_text="workshop", granite_vector=[0.1] * 3072, top_k=5, operation_id="after-failure")[0])

            # Rotation of the same personal connection remains usable.
            self.connection.api_key = "replacement-isolated-key"
            self.connection.save(update_fields=["api_key"])
            self.assertTrue(search_document_spaces(owner_id=self.user.pk, actor_id=self.user.pk, file_ids=[spin_file.pk], query="workshop", query_text="workshop", granite_vector=[0.1] * 3072, top_k=5, operation_id="rotated-key")[0])

            # External vectors cannot roll back with a surrounding SQL
            # transaction. Simulate a provider failure after deleting the
            # active generation and force the outer transaction to roll back.
            # A fresh search must report damage, not silently return no data.
            from unittest.mock import Mock
            active_generation = spin_file.index_generation
            def failing_service(*args, **kwargs):
                real_service = get_document_vector_service(*args, **kwargs)
                proxy = Mock(wraps=real_service)
                def partial_delete(key, owner):
                    result = real_service.delete_file_vectors(key, owner)
                    if key == active_generation:
                        raise RuntimeError("simulated failure after external delete")
                    return result
                proxy.delete_file_vectors.side_effect = partial_delete
                return proxy
            with self.assertRaises(SpinIndexDeletionError), transaction.atomic(), patch("files.services.spin_index_deletion.get_document_vector_service", side_effect=failing_service):
                spin_file.delete()
            spin_file.refresh_from_db()
            self.assertFalse(spin_file.spin_index_unavailable)  # marker rolled back
            with self.assertRaisesRegex(DocumentEmbeddingAccessError, "reprocess|Reprocess"):
                search_document_spaces(owner_id=self.user.pk, actor_id=self.user.pk, file_ids=[spin_file.pk], query="workshop", query_text="workshop", granite_vector=[0.1] * 3072, top_k=5, operation_id="after-outer-rollback")
            with patch("files.services.document_reprocessing_service.enqueue"):
                queued = DocumentReprocessingService().start(command)
            DocumentIngestionService().process(DocumentIngestionCommand(file_id=spin_file.pk, expected_job_id=queued.job_id, reprocessing_action="reparse", processing_mode="basic", previous_status=FileStatus.PROCESSED))
            spin_file.refresh_from_db()
            self.assertTrue(search_document_spaces(owner_id=self.user.pk, actor_id=self.user.pk, file_ids=[spin_file.pk], query="workshop", query_text="workshop", granite_vector=[0.1] * 3072, top_k=5, operation_id="after-recovery")[0])

            self.connection.delete()
            with self.assertRaises(DocumentEmbeddingAccessError) as missing:
                search_document_spaces(owner_id=self.user.pk, actor_id=self.user.pk, file_ids=[spin_file.pk], query="workshop", query_text="workshop", granite_vector=[0.1] * 3072, top_k=5, operation_id="missing-key")
            self.assertIn("Upload the original file again", str(missing.exception))
            self.assertNotIn("isolated-test-key", str(missing.exception))
            self.assertNotIn("replacement-isolated-key", str(missing.exception))

            # If the vector store is down, deleting one file is rejected and
            # a later retry removes the database row and real Weaviate index.
            target = files[2]
            target_key = target.vector_index_key
            request = self.factory.delete(f"/files/api/files/{target.pk}/")
            force_authenticate(request, user=self.user)
            partial_ack = SimpleNamespace(failed=1, matches=3, successful=2)
            with patch("weaviate.collections.data.executor._DataCollectionExecutor.delete_many", return_value=partial_ack):
                response = FileViewSet.as_view({"delete": "destroy"})(request, pk=target.pk)
            self.assertEqual(response.status_code, 503)
            self.assertTrue(File.active_objects.filter(pk=target.pk).exists())
            service = get_document_vector_service(self.user.pk, target.embedding_model, backend=VectorDBChoice.WEAVIATE)
            try:
                self.assertTrue(service.read_generation(target_key, self.user.pk, target.pk))
            finally:
                service.close()
            request = self.factory.delete(f"/files/api/files/{target.pk}/")
            force_authenticate(request, user=self.user)
            response = FileViewSet.as_view({"delete": "destroy"})(request, pk=target.pk)
            self.assertEqual(response.status_code, 204)
            self.assertFalse(File.active_objects.filter(pk=target.pk).exists())
            service = get_document_vector_service(self.user.pk, target.embedding_model, backend=VectorDBChoice.WEAVIATE)
            try:
                self.assertEqual(service.read_generation(target_key, self.user.pk, target.pk), [])
            finally:
                service.close()

            # A non-raising partial account purge must block account deletion.
            with patch("weaviate.collections.data.executor._DataCollectionExecutor.delete_many", return_value=partial_ack), self.assertRaises(AccountDeletionBlocked):
                AccountDeletionService().expunge(self.user)
            self.assertTrue(get_user_model().objects.filter(pk=self.user.pk).exists())
            self.assertTrue(File.active_objects.filter(pk=spin_file.pk).exists())

            # Bulk deletion reports partial completion honestly and permits retry.
            request = self.factory.post("/files/api/files/bulk-delete/", {"fileIds": [files[0].pk, spin_file.pk]}, format="json")
            force_authenticate(request, user=self.user)
            with patch("files.services.spin_index_deletion.get_document_vector_service", side_effect=RuntimeError("simulated outage")), patch("files.signals.delete_file_vectors.delay", return_value=None):
                response = FileViewSet.as_view({"post": "bulk_delete"})(request)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.data["deleted_count"], 1)
            self.assertEqual(response.data["failed_count"], 1)
            self.assertTrue(File.active_objects.filter(pk=spin_file.pk).exists())
            request = self.factory.post("/files/api/files/bulk-delete/", {"fileIds": [spin_file.pk]}, format="json")
            force_authenticate(request, user=self.user)
            response = FileViewSet.as_view({"post": "bulk_delete"})(request)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data["deleted_count"], 1)
            self.assertFalse(File.active_objects.filter(pk=spin_file.pk).exists())

            # Account deletion must purge every embedding space, even if a
            # queued per-file cleanup is unavailable.
            user_id = self.user.pk
            with patch("users.services.account_deletion_service.SocraticBooksClient.delete_user", return_value=True), patch("files.signals.delete_file_vectors.delay", return_value=None):
                AccountDeletionService().expunge(self.user)
            for file in files:
                service = get_document_vector_service(user_id, file.embedding_model, backend=VectorDBChoice.WEAVIATE)
                try:
                    self.assertEqual(service.read_generation(file.vector_index_key, user_id, file.pk), [])
                finally:
                    service.close()

