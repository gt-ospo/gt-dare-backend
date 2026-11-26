from contextlib import nullcontext
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from django.db import IntegrityError

from core.services.vector_integrity import VectorIntegrityError
from files.services.spin_document_embedding_client import (
    SpinDocumentEmbeddingClient,
    SpinEmbeddingError,
)


class SpinDocumentEmbeddingClientTests(TestCase):
    def setUp(self):
        self.user = Mock()
        self.client = SpinDocumentEmbeddingClient(
            user=self.user,
            connection_id="personal-key-id",
            model="embeddinggemma-2",
            operation_id="stable-operation",
            file_id=7,
        )
        patch(
            "files.services.spin_document_embedding_client.transaction.atomic",
            side_effect=nullcontext,
        ).start()
        self.calls = patch(
            "files.services.spin_document_embedding_client.SpinEmbeddingCall"
        ).start()
        self.openai = patch(
            "files.services.spin_document_embedding_client.OpenAI"
        ).start()
        self.resolve = patch(
            "files.services.spin_document_embedding_client.resolve_personal_spin_connection"
        ).start()
        self.addCleanup(patch.stopall)
        self.calls.objects.filter.return_value.update.return_value = 1
        self.resolve.return_value = SimpleNamespace(
            base_url="https://spin.pace.gatech.edu:4000/v1", api_key="private"
        )

    def test_orders_vectors_and_records_paid_usage(self):
        self.openai.return_value.__enter__.return_value.embeddings.create.return_value = SimpleNamespace(
            usage=SimpleNamespace(total_tokens=12),
            data=[
                SimpleNamespace(index=1, embedding=[2.0] * 768),
                SimpleNamespace(index=0, embedding=[1.0] * 768),
            ],
        )
        result = self.client.create_batch_embeddings(["one", "two"])
        self.assertEqual([embedding[0] for embedding in result], [1.0, 2.0])
        self.resolve.assert_called_once_with(self.user, "personal-key-id")
        self.calls.objects.filter.return_value.update.assert_called_once_with(
            state="complete", total_tokens=12
        )
        self.calls.objects.create.assert_called_once_with(
            user=self.user,
            file_id=7,
            operation_id="stable-operation",
            batch_number=0,
            model="embeddinggemma-2",
            connection_id="personal-key-id",
        )
        self.assertEqual(self.openai.call_args.kwargs["max_retries"], 0)

    def test_malformed_vectors_still_record_usage(self):
        self.openai.return_value.__enter__.return_value.embeddings.create.return_value = SimpleNamespace(
            usage=SimpleNamespace(total_tokens=5),
            data=[SimpleNamespace(index=0, embedding=[0.0] * 10)],
        )
        with self.assertRaises(VectorIntegrityError):
            self.client.create_embeddings("example")
        self.calls.objects.filter.return_value.update.assert_called_once_with(
            state="complete", total_tokens=5
        )

    def test_prior_attempt_prevents_network_replay(self):
        self.calls.objects.create.side_effect = IntegrityError()
        with self.assertRaises(SpinEmbeddingError):
            self.client.create_embeddings("example")
        self.openai.assert_not_called()

    def test_stable_operation_identifier_is_required(self):
        with self.assertRaises(ValueError):
            SpinDocumentEmbeddingClient(
                user=self.user,
                connection_id="id",
                model="embeddinggemma-2",
                operation_id="",
            )
