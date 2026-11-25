from unittest.mock import Mock, patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase

from files.models import File
from users.services.account_deletion_service import (
    AccountDeletionBlocked,
    AccountDeletionService,
)


class SpinAccountDeletionSafetyTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="spin-delete-owner@example.com", password="unused"
        )

    @patch("users.services.account_deletion_service.get_document_vector_service")
    def test_granite_only_account_does_not_touch_spin_collections(self, factory):
        File._base_manager.create(user=self.user, name="plain.txt")
        AccountDeletionService()._purge_spin_vector_namespaces_or_block(self.user.id)
        factory.assert_not_called()

    @patch("users.services.account_deletion_service.get_document_vector_service")
    def test_purge_failure_blocks_account_deletion_and_can_be_retried(self, factory):
        File._base_manager.create(
            user=self.user, name="spin.txt", embedding_model="embeddinggemma-2"
        )
        factory.side_effect = RuntimeError("temporary outage")
        service = AccountDeletionService()
        with patch.object(service, "_ensure_not_blocked"), patch.object(
            service, "_purge_vector_namespace"
        ) as granite_purge, patch.object(service, "_delete_file_blobs") as delete_blobs:
            with self.assertRaises(AccountDeletionBlocked):
                service.expunge(self.user)
            granite_purge.assert_not_called()
            delete_blobs.assert_not_called()
        self.assertTrue(get_user_model().objects.filter(pk=self.user.pk).exists())
        self.assertTrue(File._base_manager.filter(user=self.user).exists())
        self.assertTrue(File._base_manager.get(user=self.user).spin_index_unavailable)

        vector = Mock()
        vector.delete_namespace.return_value = True
        factory.side_effect = None
        factory.return_value = vector
        service._purge_spin_vector_namespaces_or_block(self.user.pk)
        self.assertEqual(vector.delete_namespace.call_count, 2)

    @patch("users.services.account_deletion_service.get_document_vector_service")
    def test_active_spin_processing_blocks_before_any_purge(self, factory):
        File._base_manager.create(
            user=self.user,
            name="spin.txt",
            embedding_model="embeddinggemma-2",
            ingestion_token=uuid4(),
        )
        service = AccountDeletionService()
        with patch.object(service, "_ensure_not_blocked"), patch.object(
            service, "_purge_vector_namespace"
        ) as granite_purge:
            with self.assertRaises(AccountDeletionBlocked):
                service.expunge(self.user)
            granite_purge.assert_not_called()
        factory.assert_not_called()
        self.assertTrue(get_user_model().objects.filter(pk=self.user.pk).exists())
