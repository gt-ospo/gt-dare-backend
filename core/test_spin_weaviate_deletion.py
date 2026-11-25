from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from core.helpers.weaviate import WeaviateClient


class SpinWeaviateDeletionTests(TestCase):
    def setUp(self):
        self.client = WeaviateClient.__new__(WeaviateClient)
        self.client.collection_name = "DocumentSpin"
        self.client.strict_deletion = True
        self.collection = Mock()
        self.client.client = Mock()
        self.client.client.collections.get.return_value = self.collection
        self.collection.query.fetch_objects.return_value = SimpleNamespace(objects=[])

    def delete(self, action):
        if action == "file":
            return self.client.delete_file_vectors("generation", 4)
        return self.client.delete_namespace("user_4")

    def test_success_and_already_empty_require_readback(self):
        for action in ("file", "account"):
            for count in (0, 3):
                with self.subTest(action=action, count=count):
                    self.collection.data.delete_many.return_value = SimpleNamespace(
                        failed=0, matches=count, successful=count
                    )
                    self.assertTrue(self.delete(action))
                    self.collection.query.fetch_objects.assert_called()
                    self.collection.query.fetch_objects.reset_mock()

    def test_nonraising_partial_failures_are_rejected(self):
        for action in ("file", "account"):
            for response in (
                SimpleNamespace(failed=1, matches=3, successful=2),
                SimpleNamespace(failed=0, matches=3, successful=2),
                SimpleNamespace(failed=0, matches=2, successful=3),
                SimpleNamespace(failed=None, matches=0, successful=0),
                None,
            ):
                with self.subTest(action=action, response=response):
                    self.collection.data.delete_many.return_value = response
                    with self.assertRaisesRegex(Exception, "could not be confirmed"):
                        self.delete(action)

    def test_acknowledged_deletion_with_residual_objects_is_rejected(self):
        self.collection.data.delete_many.return_value = SimpleNamespace(
            failed=0, matches=1, successful=1
        )
        self.collection.query.fetch_objects.return_value = SimpleNamespace(objects=[object()])
        for action in ("file", "account"):
            with self.subTest(action=action):
                with self.assertRaisesRegex(Exception, "left matching objects"):
                    self.delete(action)

    def test_legacy_deletion_does_not_change(self):
        self.client.strict_deletion = False
        self.collection.data.delete_many.return_value = None
        self.assertTrue(self.delete("file"))
        self.assertTrue(self.delete("account"))
        self.collection.query.fetch_objects.assert_not_called()
