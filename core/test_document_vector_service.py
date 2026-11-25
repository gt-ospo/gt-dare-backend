from unittest import TestCase
from unittest.mock import patch

from django.test import override_settings

from core.services.vector_service import get_document_vector_service
from users.constants import VectorDBChoice


class DocumentVectorServiceTests(TestCase):
    @patch("core.services.vector_service.get_vector_service")
    def test_granite_keeps_existing_backend(self, existing_service):
        self.assertIs(
            get_document_vector_service(4, "granite", backend=VectorDBChoice.WEAVIATE),
            existing_service.return_value,
        )
        existing_service.assert_called_once_with(4, backend=VectorDBChoice.WEAVIATE)

    @override_settings(WEAVIATE={"COLLECTION_NAME": "Document"})
    @patch("core.services.vector_service.WeaviateVectorService")
    def test_each_spin_model_has_its_own_collection(self, service):
        get_document_vector_service(
            4, "embeddinggemma-2", backend=VectorDBChoice.WEAVIATE
        )
        service.assert_called_with(
            collection_name="DocumentSpinEmbeddingGemma2", strict_deletion=True
        )
        get_document_vector_service(
            4, "Nemotron-3.5-Embed-8B", backend=VectorDBChoice.WEAVIATE
        )
        service.assert_called_with(
            collection_name="DocumentSpinNemotron35Embed8B", strict_deletion=True
        )

    def test_rejects_unknown_models_and_unsupported_backends(self):
        with self.assertRaises(ValueError):
            get_document_vector_service(
                4, "unrecognized", backend=VectorDBChoice.WEAVIATE
            )
        with self.assertRaises(ValueError):
            get_document_vector_service(
                4, "embeddinggemma-2", backend=VectorDBChoice.PINECONE
            )
