from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework.test import APITestCase
from core.services.document_embedding_profiles import GRANITE
from core.services.file_upload_service import FileUploadService
from files.constants import DocumentProcessingMode, FileStatus
from files.models import File
from files.services.document_embedding_search import (
    DocumentEmbeddingAccessError,
    search_document_spaces,
)


@override_settings(SPIN_DOCUMENT_EMBEDDINGS_ENABLED=True)
class GraniteUploadPolicyReview(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="granite-owner@example.test", password="synthetic"
        )
        self.other = get_user_model().objects.create_user(
            email="granite-other@example.test", password="synthetic"
        )
        self.client.force_authenticate(self.user)
        self.tmp = TemporaryDirectory(prefix="granite-upload-policy-")
        self.addCleanup(self.tmp.cleanup)
        self.media = override_settings(MEDIA_ROOT=self.tmp.name)
        self.media.enable()
        self.addCleanup(self.media.disable)

    def upload(self, **fields):
        data = {
            "files": SimpleUploadedFile(
                "sample.txt", b"synthetic training document", content_type="text/plain"
            ),
            "names": "sample.txt",
            **fields,
        }
        with patch(
            "core.services.file_upload_service.FileUploadService.enqueue_processing"
        ) as enqueue, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post("/api/files/", data, format="multipart")
        return response, enqueue

    def test_normal_and_explicit_granite_upload(self):
        for fields in ({}, {"embedding_model": GRANITE}):
            with self.subTest(fields=fields):
                response, enqueue = self.upload(**fields)
                self.assertEqual(response.status_code, 201, response.data)
                file = File.active_objects.get(pk=response.data[0]["id"])
                self.assertEqual(file.embedding_model, GRANITE)
                self.assertIsNone(file.embedding_connection_id)
            enqueue.assert_called_once()

    def test_spin_upload_rejected_before_persist(self):
        for model in ("embeddinggemma-2", "Nemotron-3.5-Embed-8B"):
            with self.subTest(model=model):
                before = File._base_manager.count()
                response, enqueue = self.upload(
                    embedding_model=model, embedding_connection_id=str(uuid4())
                )
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn("embedding_model", response.data)
                self.assertIn("Granite", str(response.data))
                self.assertEqual(File._base_manager.count(), before)
                enqueue.assert_not_called()

    def test_rejected_document_never_resolves_personal_key(self):
        with patch(
            "files.api.serializers.resolve_personal_spin_connection"
        ) as resolver:
            response, _ = self.upload(
                embedding_model="embeddinggemma-2", embedding_connection_id=str(uuid4())
            )
        self.assertEqual(response.status_code, 400, response.data)
        resolver.assert_not_called()

    def test_direct_batch_spin_document_rejected_without_any_file(self):
        doc = SimpleUploadedFile("direct.txt", b"synthetic", content_type="text/plain")
        with self.assertRaisesRegex(ValueError, "default Granite"):
            FileUploadService.upload_files(
                [doc],
                ["direct.txt"],
                self.user,
                embedding_model="embeddinggemma-2",
                embedding_connection_id=uuid4(),
            )
        self.assertFalse(File._base_manager.exists())

    def test_direct_mixed_batch_rejects_before_writing_media_bytes(self):
        media = SimpleUploadedFile("photo.png", b"picture", content_type="image/png")
        doc = SimpleUploadedFile("doc.txt", b"document", content_type="text/plain")
        with patch("core.storage.fields.DynamicStorageFieldFile.save") as save:
            with self.assertRaisesRegex(ValueError, "default Granite"):
                FileUploadService.upload_files(
                    [media, doc],
                    ["photo.png", "doc.txt"],
                    self.user,
                    embedding_model="embeddinggemma-2",
                    embedding_connection_id=uuid4(),
                )
        save.assert_not_called()
        self.assertFalse(File._base_manager.exists())

    def test_granite_with_connection_is_rejected(self):
        response, enqueue = self.upload(
            embedding_model=GRANITE, embedding_connection_id=str(uuid4())
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("embedding_connection_id", response.data)
        self.assertFalse(File._base_manager.exists())
        enqueue.assert_not_called()

    def test_direct_service_override_rejected_before_file_creation(self):
        file = SimpleUploadedFile("direct.txt", b"synthetic", content_type="text/plain")
        with self.assertRaisesRegex(ValueError, "default Granite"):
            FileUploadService.create_file_instance(
                file,
                "direct.txt",
                self.user,
                embedding_model="embeddinggemma-2",
                embedding_connection_id=uuid4(),
            )
        self.assertFalse(File._base_manager.exists())

    def test_anonymous_upload_is_denied(self):
        self.client.force_authenticate(None)
        response, _ = self.upload()
        self.assertIn(response.status_code, (401, 403))
        self.assertFalse(File._base_manager.exists())

    def legacy_spin_file(self):
        return File.active_objects.create(
            user=self.user,
            name="legacy.txt",
            file="files/legacy.txt",
            status=FileStatus.PROCESSED,
            processing_mode=DocumentProcessingMode.BASIC,
            embedding_model="embeddinggemma-2",
            embedding_connection_id=uuid4(),
            index_generation="synthetic-generation",
            extracted_text="synthetic",
        )

    def test_existing_spin_reprocessing_and_owner_scope(self):
        file = self.legacy_spin_file()
        old = (
            file.embedding_model,
            file.embedding_connection_id,
            file.index_generation,
        )
        url = f"/api/files/{file.pk}/reprocess/"
        self.client.force_authenticate(self.other)
        with patch("files.services.document_reprocessing_service.enqueue") as enqueue:
            response = self.client.post(
                url, {"action": "reparse", "processingMode": "basic"}, format="json"
            )
        self.assertEqual(response.status_code, 404, response.data)
        enqueue.assert_not_called()
        self.client.force_authenticate(self.user)
        with patch("files.services.document_reprocessing_service.enqueue") as enqueue:
            response = self.client.post(
                url, {"action": "reparse", "processingMode": "basic"}, format="json"
            )
        self.assertEqual(response.status_code, 202, response.data)
        enqueue.assert_called_once()
        file.refresh_from_db()
        self.assertEqual(
            (file.embedding_model, file.embedding_connection_id, file.index_generation),
            old,
        )

    def test_existing_spin_search_uses_original_space_and_owner_gate(self):
        file = self.legacy_spin_file()
        args = dict(
            owner_id=self.user.pk,
            file_ids=[file.pk],
            query="synthetic",
            query_text="synthetic",
            granite_vector=[0.1],
            top_k=3,
            operation_id="synthetic-op",
        )
        with patch(
            "core.services.index_health.check_index_health",
            return_value=SimpleNamespace(state="verified"),
        ), patch(
            "files.services.document_embedding_search.SpinDocumentEmbeddingClient"
        ) as client, patch(
            "files.services.document_embedding_search.get_document_vector_service"
        ) as service:
            client.return_value.create_embeddings.return_value = [0.2]
            service.return_value.search_documents.return_value = [{"synthetic": True}]
            result = search_document_spaces(actor_id=self.user.pk, **args)
            self.assertEqual(result, [[{"synthetic": True}]])
            self.assertEqual(client.call_args.kwargs["model"], file.embedding_model)
            self.assertEqual(
                client.call_args.kwargs["connection_id"], file.embedding_connection_id
            )
            client.reset_mock()
            with self.assertRaises(DocumentEmbeddingAccessError):
                search_document_spaces(actor_id=self.other.pk, **args)
            client.assert_not_called()

    def test_media_only_keeps_legacy_behavior_without_embedding(self):
        from users.constants import VectorDBChoice

        self.user.vector_db = VectorDBChoice.WEAVIATE
        self.user.save(update_fields=["vector_db"])
        connection = uuid4()
        data = {
            "files": SimpleUploadedFile(
                "photo.png", b"synthetic-image", content_type="image/png"
            ),
            "names": "photo.png",
            "embedding_model": "embeddinggemma-2",
            "embedding_connection_id": str(connection),
        }
        with patch(
            "files.api.serializers.resolve_personal_spin_connection",
            return_value=SimpleNamespace(),
        ), patch(
            "core.services.file_upload_service.FileUploadService.enqueue_processing"
        ) as enqueue:
            response = self.client.post("/api/files/", data, format="multipart")
        self.assertEqual(response.status_code, 201, response.data)
        file = File.active_objects.get(pk=response.data[0]["id"])
        self.assertTrue(file.is_media)
        self.assertEqual(file.embedding_model, GRANITE)
        self.assertIsNone(file.embedding_connection_id)
        enqueue.assert_not_called()

    def test_direct_media_upload_with_stale_spin_has_no_embedding(self):
        upload = SimpleUploadedFile(
            "photo.png", b"synthetic-image", content_type="image/png"
        )
        with patch(
            "core.services.file_upload_service.FileUploadService.enqueue_processing"
        ) as enqueue:
            file = FileUploadService.create_file_instance(
                upload,
                "photo.png",
                self.user,
                embedding_model="embeddinggemma-2",
                embedding_connection_id=uuid4(),
            )
        self.assertTrue(file.is_media)
        self.assertEqual(file.embedding_model, GRANITE)
        self.assertIsNone(file.embedding_connection_id)
        enqueue.assert_not_called()

    def test_mixed_media_document_spin_batch_rejected_without_writes(self):
        data = {
            "files": [
                SimpleUploadedFile("photo.png", b"image", content_type="image/png"),
                SimpleUploadedFile("doc.txt", b"doc", content_type="text/plain"),
            ],
            "names": ["photo.png", "doc.txt"],
            "embedding_model": "embeddinggemma-2",
            "embedding_connection_id": str(uuid4()),
        }
        response = self.client.post("/api/files/", data, format="multipart")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(File._base_manager.exists())

    def test_mime_classification_matches_service_even_with_misleading_filename(self):
        upload = SimpleUploadedFile(
            "looks-like-a-document.txt", b"synthetic", content_type="image/png"
        )
        self.assertEqual(
            FileUploadService.detect_media_type(upload.content_type)[0], True
        )
        with patch(
            "core.services.file_upload_service.FileUploadService.enqueue_processing"
        ) as enqueue:
            file = FileUploadService.create_file_instance(
                upload,
                upload.name,
                self.user,
                embedding_model="embeddinggemma-2",
                embedding_connection_id=uuid4(),
            )
        self.assertTrue(file.is_media)
        self.assertEqual(file.embedding_model, GRANITE)
        self.assertIsNone(file.embedding_connection_id)
        enqueue.assert_not_called()

    def test_camelcase_and_conflicting_model_fields_rejected(self):
        response, _ = self.upload(
            embeddingModel="embeddinggemma-2", embeddingConnectionId=str(uuid4())
        )
        self.assertEqual(response.status_code, 400, response.data)
        response, _ = self.upload(
            embedding_model=GRANITE, embeddingModel="embeddinggemma-2"
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(File._base_manager.exists())

    def test_empty_folder_with_stale_embedding_fields_remains_allowed(self):
        response = self.client.post(
            "/api/folders/",
            {
                "name": "synthetic-empty",
                "embeddingModel": "embeddinggemma-2",
                "embeddingConnectionId": str(uuid4()),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertFalse(File._base_manager.exists())

    def test_folder_and_internal_upload_reject_spin_before_creating_rows(self):
        file = lambda: SimpleUploadedFile(
            "folder.txt", b"doc", content_type="text/plain"
        )
        data = {
            "name": "synthetic-folder",
            "files": file(),
            "names": "folder.txt",
            "embedding_model": "embeddinggemma-2",
            "embedding_connection_id": str(uuid4()),
        }
        response = self.client.post("/api/folders/", data, format="multipart")
        self.assertEqual(response.status_code, 400, response.data)
        from files.models import Folder

        self.assertFalse(
            Folder.objects.filter(user=self.user, name="synthetic-folder").exists()
        )
        self.assertFalse(File._base_manager.exists())
        with override_settings(DARE_INTERNAL_KEY="synthetic-internal-key"):
            data = {
                "user_id": str(self.user.pk),
                "files": file(),
                "names": "folder.txt",
                "embeddingModel": "embeddinggemma-2",
                "embeddingConnectionId": str(uuid4()),
            }
            response = self.client.post(
                "/api/internal/upload/",
                data,
                format="multipart",
                HTTP_X_INTERNAL_KEY="synthetic-internal-key",
            )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(File._base_manager.exists())
