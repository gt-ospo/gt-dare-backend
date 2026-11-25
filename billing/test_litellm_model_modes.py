from types import SimpleNamespace
from unittest.mock import patch

import httpx
from django.test import SimpleTestCase

from billing.litellm_models_service import CachedProbe
from billing.litellm_probe import ProbedModel, probe_litellm_connection
from conversations.services.llm_filter_service import _filter_for_litellm


class LiteLLMModelModesTests(SimpleTestCase):
    def response(self, data, status=200):
        return httpx.Response(status, request=httpx.Request("GET", "https://spin.example.test/"), json=data)

    def test_mixed_modes_filter_chat_but_preserve_catalogue(self):
        models = self.response({"data": [{"id": "chat"}, {"id": "embed"}, {"id": "mixed"}, {"id": "unknown"}]})
        info = self.response({"data": [
            {"model_name": "chat", "model_info": {"mode": "chat"}},
            {"model_name": "embed", "model_info": {"mode": "embedding"}},
            {"model_name": "mixed", "model_info": {"mode": "embedding"}},
            {"model_name": "mixed", "model_info": {"mode": "chat"}},
        ]})
        with patch("billing.litellm_probe.httpx.get", side_effect=[models, info]) as get:
            result = probe_litellm_connection("https://spin.example.test:4000/v1", "secret")
        self.assertEqual(result.model_names, ["chat", "embed", "mixed", "unknown"])
        self.assertEqual(result.chat_model_names, ["chat", "mixed", "unknown"])
        self.assertEqual(get.call_args.args[0], "https://spin.example.test:4000/model/info")
        self.assertFalse(get.call_args.kwargs["follow_redirects"])
        with patch("conversations.services.llm_filter_service.litellm_models_service.list_models", return_value=CachedProbe(result.models, False, None)):
            entries, _ = _filter_for_litellm(SimpleNamespace(pk="test"))
        self.assertEqual([entry["name"] for entry in entries], result.chat_model_names)

    def test_metadata_failure_preserves_catalogue(self):
        for error in (httpx.TimeoutException("timeout"), self.response({}, 403), self.response([])):
            with self.subTest(error=error):
                models = self.response({"data": [{"id": "legacy"}]})
                with patch("billing.litellm_probe.httpx.get", side_effect=[models, error]):
                    result = probe_litellm_connection("https://example.test/v1", "secret")
                self.assertTrue(result.ok)
                self.assertEqual(result.chat_model_names, ["legacy"])

    def test_old_cached_probed_model_has_unknown_mode(self):
        old = ProbedModel("legacy")
        object.__delattr__(old, "supports_chat")
        with patch("conversations.services.llm_filter_service.litellm_models_service.list_models", return_value=CachedProbe([old], True, None)):
            entries, metadata = _filter_for_litellm(SimpleNamespace(pk="test"))
        self.assertEqual([entry["name"] for entry in entries], ["legacy"])
        self.assertTrue(metadata.stale_probe)
