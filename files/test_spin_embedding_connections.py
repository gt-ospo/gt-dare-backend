from unittest import TestCase
from unittest.mock import Mock, patch

from billing.constants import LiteLLMKeySourceChoice
from files.services.spin_embedding_connections import (
    SpinEmbeddingConnectionUnavailable,
    personal_spin_connections,
    resolve_personal_spin_connection,
)


class PersonalSpinConnectionTests(TestCase):
    @patch("files.services.spin_embedding_connections.LiteLLMKey")
    def test_selection_is_limited_to_owners_personal_visible_keys(self, key_model):
        user = Mock()
        personal_spin_connections(user)
        key_model.visible_for_user.assert_called_once_with(user)
        key_model.visible_for_user.return_value.filter.assert_called_once_with(
            source=LiteLLMKeySourceChoice.USER, owner_user=user
        )

    @patch("files.services.spin_embedding_connections.personal_spin_connections")
    def test_missing_and_non_spin_keys_cannot_be_used(self, personal_connections):
        user = Mock()
        lookup = personal_connections.return_value.filter.return_value
        for connection in (None, Mock(base_url="https://elsewhere.invalid/v1")):
            lookup.first.return_value = connection
            with self.subTest(connection=connection):
                with self.assertRaises(SpinEmbeddingConnectionUnavailable):
                    resolve_personal_spin_connection(user, "connection-id")

    @patch("files.services.spin_embedding_connections.personal_spin_connections")
    def test_owner_can_use_eligible_spin_key(self, personal_connections):
        user = Mock()
        connection = Mock(base_url="https://spin.pace.gatech.edu:4000/v1")
        personal_connections.return_value.filter.return_value.first.return_value = (
            connection
        )
        self.assertIs(
            resolve_personal_spin_connection(user, "connection-id"), connection
        )
