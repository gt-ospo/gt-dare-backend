from unittest import TestCase

from core.services.document_embedding_profiles import SPIN_PROFILES, is_spin_base_url
from core.services.vector_integrity import VectorIntegrityError, validate_embedding


class DocumentEmbeddingProfileTests(TestCase):
    def test_profile_dimensions_and_spaces_are_distinct(self):
        gemma = SPIN_PROFILES["embeddinggemma-2"]
        nemotron = SPIN_PROFILES["Nemotron-3.5-Embed-8B"]
        self.assertEqual((gemma.dimensions, nemotron.dimensions), (768, 4096))
        self.assertNotEqual(gemma.collection_suffix, nemotron.collection_suffix)

    def test_only_the_exact_spin_origin_is_accepted(self):
        self.assertTrue(is_spin_base_url("https://spin.pace.gatech.edu:4000/v1"))
        self.assertTrue(is_spin_base_url("https://spin.pace.gatech.edu:4000/v1/"))
        for url in (
            "http://spin.pace.gatech.edu:4000/v1",
            "https://spin.pace.gatech.edu.evil.example:4000/v1",
            "https://user@spin.pace.gatech.edu:4000/v1",
            "https://spin.pace.gatech.edu:4000/v1?proxy=evil",
            "https://spin.pace.gatech.edu:4000/v1#fragment",
            "https://spin.pace.gatech.edu:invalid/v1",
        ):
            with self.subTest(url=url):
                self.assertFalse(is_spin_base_url(url))

    def test_dimension_validation_retains_legacy_default(self):
        validate_embedding([0.0] * 3072)
        validate_embedding([0.0] * 768, dimension=768)
        with self.assertRaises(VectorIntegrityError):
            validate_embedding([0.0] * 768)
        with self.assertRaises(VectorIntegrityError):
            validate_embedding([0.0] * 768, dimension=4096)
