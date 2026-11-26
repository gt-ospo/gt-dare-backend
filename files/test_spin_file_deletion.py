from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase

from files.models import File, VectorIndexAttempt
from files.services.spin_index_deletion import SpinIndexBusyError, SpinIndexDeletionError
from users.constants import VectorDBChoice


class SpinFileDeletionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="spin-file-delete@example.com", password="unused"
        )
        self.file = File._base_manager.create(
            user=self.user,
            name="document.txt",
            file="document.txt",
            embedding_model="embeddinggemma-2",
            index_generation="generation123",
            vector_db_source=VectorDBChoice.WEAVIATE,
        )

    @patch("core.storage.fields.DynamicStorageFieldFile.delete")
    @patch("files.services.spin_index_deletion.get_document_vector_service")
    def test_failure_keeps_row_and_original_until_retry(self, factory, delete_blob):
        factory.side_effect = RuntimeError("temporarily unavailable")
        with self.assertRaises(SpinIndexDeletionError):
            self.file.delete()
        self.assertTrue(File._base_manager.filter(pk=self.file.pk).exists())
        self.file.refresh_from_db()
        self.assertTrue(self.file.spin_index_unavailable)
        self.assertIn("reprocess using Basic", self.file.error_message)
        delete_blob.assert_not_called()
        factory.side_effect = None
        factory.return_value.delete_file_vectors.return_value = True
        self.file.delete()
        self.assertFalse(File._base_manager.filter(user=self.user).exists())
        delete_blob.assert_called_once_with(save=False)

    @patch("files.services.spin_index_deletion.get_document_vector_service")
    def test_active_lease_blocks_deletion_without_touching_index(self, factory):
        self.file.ingestion_token = uuid4()
        self.file.save(update_fields=["ingestion_token"])
        with self.assertRaises(SpinIndexBusyError):
            self.file.delete()
        self.file.refresh_from_db()
        self.assertFalse(self.file.spin_index_unavailable)
        factory.assert_not_called()

    @patch("files.services.spin_index_deletion.get_document_vector_service")
    def test_uncertain_attempts_block_even_after_lease_clears(self, factory):
        attempt = VectorIndexAttempt.objects.create(
            file=self.file,
            generation="unfinishedgeneration",
            owner_id=self.user.pk,
            backend=VectorDBChoice.WEAVIATE,
            embedding_model="embeddinggemma-2",
            status="abandoned",
        )
        with self.assertRaises(SpinIndexBusyError):
            self.file.delete()
        attempt.status = "failed"
        attempt.attempted_count = 1
        attempt.save(update_fields=["status", "attempted_count"])
        with self.assertRaises(SpinIndexBusyError):
            self.file.delete()
        factory.assert_not_called()

