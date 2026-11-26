from types import SimpleNamespace
from unittest import TestCase

from core.services.document_processor import document_target_backend
from users.constants import VectorDBChoice


class DocumentTargetBackendTests(TestCase):
    def test_granite_reprocessing_follows_current_user_preference(self):
        file = SimpleNamespace(
            embedding_model="granite",
            vector_db_source=VectorDBChoice.WEAVIATE,
            user=SimpleNamespace(vector_db=VectorDBChoice.PINECONE),
        )
        self.assertEqual(document_target_backend(file), VectorDBChoice.PINECONE)

    def test_spin_remains_weaviate_when_user_preference_changes(self):
        file = SimpleNamespace(
            embedding_model="embeddinggemma-2",
            vector_db_source=VectorDBChoice.WEAVIATE,
            user=SimpleNamespace(vector_db=VectorDBChoice.PINECONE),
        )
        self.assertEqual(document_target_backend(file), VectorDBChoice.WEAVIATE)
        file.vector_db_source = None
        self.assertEqual(document_target_backend(file), VectorDBChoice.WEAVIATE)

    def test_spin_does_not_silently_migrate_an_invalid_index(self):
        file = SimpleNamespace(
            embedding_model="embeddinggemma-2",
            vector_db_source=VectorDBChoice.PINECONE,
            user=SimpleNamespace(vector_db=VectorDBChoice.WEAVIATE),
        )
        with self.assertRaises(ValueError):
            document_target_backend(file)
