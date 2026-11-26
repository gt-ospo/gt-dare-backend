from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from files.services.document_embedding_search import (
    DocumentEmbeddingAccessError,
    interleave_space_rankings,
    search_document_spaces,
)


class DocumentEmbeddingSearchTests(TestCase):
    def setUp(self):
        self.file = SimpleNamespace(
            pk=7,
            user_id=1,
            user=Mock(),
            embedding_model="embeddinggemma-2",
            embedding_connection_id="connection",
            vector_db_source=1,
            spin_index_unavailable=False,
        )
        self.files = patch("files.services.document_embedding_search.File").start()
        self.files.active_objects.filter.return_value.select_related.return_value.order_by.return_value = [
            self.file
        ]
        self.client = patch(
            "files.services.document_embedding_search.SpinDocumentEmbeddingClient"
        ).start()
        self.vector = patch(
            "files.services.document_embedding_search.get_document_vector_service"
        ).start()
        self.health = patch("core.services.index_health.check_index_health").start()
        self.health.return_value.state = "verified"
        self.addCleanup(patch.stopall)
        self.args = dict(
            owner_id=1,
            actor_id=1,
            file_ids=[7],
            query="question",
            query_text="question",
            granite_vector=[0.0] * 3072,
            top_k=3,
            operation_id="message:42",
        )

    def test_denies_missing_or_different_actor_without_touching_key(self):
        for actor_id in (None, 2):
            with self.subTest(actor_id=actor_id), self.assertRaises(
                DocumentEmbeddingAccessError
            ):
                search_document_spaces(**{**self.args, "actor_id": actor_id})
        self.client.assert_not_called()
        self.vector.assert_not_called()

    def test_search_uses_users_pinned_model_and_key(self):
        self.client.return_value.create_embeddings.return_value = [1.0] * 768
        self.vector.return_value.search_documents.return_value = [{"id": "chunk"}]
        result = search_document_spaces(**self.args)
        self.assertEqual(result, [[{"id": "chunk"}]])
        self.assertEqual(self.client.call_args.kwargs["connection_id"], "connection")
        self.assertEqual(self.client.call_args.kwargs["model"], "embeddinggemma-2")
        self.vector.return_value.close.assert_called_once()

    def test_repeated_identical_searches_are_distinct_operations(self):
        self.client.return_value.create_embeddings.return_value = [1.0] * 768
        self.vector.return_value.search_documents.return_value = []
        search_document_spaces(**self.args)
        search_document_spaces(**self.args)
        first, second = self.client.call_args_list
        self.assertNotEqual(
            first.kwargs["operation_id"], second.kwargs["operation_id"]
        )

    def test_partially_deleted_index_is_not_searched(self):
        self.file.spin_index_unavailable = True
        with self.assertRaisesRegex(DocumentEmbeddingAccessError, "Reprocess"):
            search_document_spaces(**self.args)
        self.client.assert_not_called()
        self.vector.assert_not_called()

    def test_actual_incomplete_index_is_not_searched_without_a_marker(self):
        self.health.return_value.state = "incomplete"
        with self.assertRaisesRegex(DocumentEmbeddingAccessError, "Reprocess"):
            search_document_spaces(**self.args)
        self.client.assert_not_called()
        self.vector.assert_not_called()

    def test_fusion_uses_rank_instead_of_incomparable_scores(self):
        first = [{"id": "a", "score": 0.1}, {"id": "b", "score": 0.05}]
        second = [{"id": "c", "score": 0.99}, {"id": "d", "score": 0.9}]
        self.assertEqual(
            [m["id"] for m in interleave_space_rankings([first, second], 3)],
            ["a", "c", "b"],
        )
        self.assertEqual(interleave_space_rankings([first, second], 0), [])
