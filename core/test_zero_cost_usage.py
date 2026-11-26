from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless

from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient

from api_keys.constants import BillingModeChoice
from billing.constants import TransactionSourceChoice, TransactionTypeChoice
from billing.models import MessageUsageAttempt, Transaction, Wallet
from conversations.models import Conversation, LLM, Message
from core.services.billing_service import BillingService, tracks_free_dare_wallet_usage
from users.models import User


class ZeroCostUsageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="zero-usage@example.test", password="test")
        self.wallet, _ = Wallet.objects.update_or_create(
            user=self.user, defaults={"balance": Decimal("5.000000")}
        )
        self.user.refresh_from_db()
        self.model = LLM.objects.create(
            name="Free test model", identifier="zero-usage-test-model", provider="custom",
            input_token_rate_per_million=Decimal("0"),
            output_token_rate_per_million=Decimal("0"),
        )
        self.conversation = Conversation.active_objects.create(user=self.user)
        self.message = Message.active_objects.create(
            conversation=self.conversation, sender_type=2, llm=self.model, message=""
        )
        self.service = BillingService()

    def usage(self):
        return Transaction.objects.filter(user=self.user, source=TransactionSourceChoice.USAGE)

    def test_zero_rate_usage_is_visible_without_changing_balance(self):
        before = self.wallet.balance
        self.service.finalize_ai_message(
            self.message, "READY", {"input_tokens": 22, "output_tokens": 35}
        )
        row = self.usage().get()
        self.assertEqual((row.input_tokens, row.output_tokens), (22, 35))
        self.assertEqual(row.amount, Decimal("0"))
        self.assertEqual(row.type, TransactionTypeChoice.DEBIT)
        self.assertEqual(row.llm, self.model)
        self.assertEqual(row.billing_mode, BillingModeChoice.WALLET)
        self.assertEqual(row.platform, self.conversation.source)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, before)

    def test_same_attempt_retry_records_once(self):
        self.message._usage_attempt_id = MessageUsageAttempt.objects.create(message=self.message).pk
        for _ in range(2):
            self.service.finalize_ai_message(
                self.message, "READY", {"input_tokens": 22, "output_tokens": 35}
            )
        self.assertEqual(self.usage().count(), 1)


    def test_regeneration_is_a_separate_attempt(self):
        for _ in range(2):
            self.message._usage_attempt_id = MessageUsageAttempt.objects.create(message=self.message).pk
            self.service.finalize_ai_message(
                self.message, "READY", {"input_tokens": 22, "output_tokens": 35}
            )
        self.assertEqual(self.usage().count(), 2)
        self.assertEqual(sum(row.input_tokens for row in self.usage()), 44)

    def test_reloaded_message_replays_with_durable_attempt_id(self):
        first = MessageUsageAttempt.objects.create(message=self.message).pk
        self.service.finalize_ai_message(self.message, "first", {"input_tokens": 22, "output_tokens": 35}, usage_attempt_id=first)
        second = MessageUsageAttempt.objects.create(message=self.message).pk
        self.service.finalize_ai_message(self.message, "regeneration", {"input_tokens": 5, "output_tokens": 9}, usage_attempt_id=second)
        reloaded = Message.active_objects.get(pk=self.message.pk)
        self.service.finalize_ai_message(reloaded, "first replay", {"input_tokens": 22, "output_tokens": 35}, usage_attempt_id=first)
        self.assertEqual(self.usage().count(), 2)
        reloaded.refresh_from_db()
        self.assertEqual(reloaded.message, "regeneration")

    def test_reloaded_ambiguous_attempt_requires_identifier(self):
        from django.core.exceptions import ValidationError
        MessageUsageAttempt.objects.create(message=self.message)
        MessageUsageAttempt.objects.create(message=self.message)
        reloaded = Message.active_objects.get(pk=self.message.pk)
        with self.assertRaises(ValidationError):
            self.service.finalize_ai_message(reloaded, "ambiguous", {"input_tokens": 1, "output_tokens": 1})
        self.assertFalse(self.usage().exists())

    def test_no_observed_usage_does_not_create_a_transaction(self):
        self.service.finalize_ai_message(self.message, "error", {})
        self.service.finalize_ai_message(
            self.message, "error", {"input_tokens": 0, "output_tokens": 0}
        )
        self.assertFalse(self.usage().exists())

    def test_partial_response_usage_is_recorded(self):
        self.service.finalize_ai_message(
            self.message, "Generation stopped", {"input_tokens": 7, "output_tokens": 3}
        )
        row = self.usage().get()
        self.assertEqual((row.input_tokens, row.output_tokens), (7, 3))

    def test_personal_api_zero_cost_preserves_existing_behavior(self):
        self.user.billing_mode = BillingModeChoice.OWN_API
        self.user.save(update_fields=["billing_mode"])
        self.service.finalize_ai_message(
            self.message, "READY", {"input_tokens": 1, "output_tokens": 1}
        )
        self.assertFalse(self.usage().exists())
        self.assertFalse(MessageUsageAttempt.objects.filter(message=self.message).exists())

    def test_attempts_exclude_paid_proxy_bot_and_external_calls(self):
        eligible = lambda: tracks_free_dare_wallet_usage(self.conversation, self.user, self.message, self.model)
        self.assertTrue(eligible())
        self.model.input_token_rate_per_million = Decimal("1")
        self.assertFalse(eligible())
        self.model.input_token_rate_per_million = Decimal("0")
        self.model.cached_input_token_rate_per_million = Decimal("0.1")
        self.assertFalse(eligible())
        self.model.cached_input_token_rate_per_million = None
        self.message.litellm_key_id = 1
        self.assertFalse(eligible())
        self.message.litellm_key_id = None
        self.conversation.bot_id = 1
        self.assertFalse(eligible())
        self.conversation.bot_id = None
        self.user.billing_mode = BillingModeChoice.OWN_API
        self.assertFalse(eligible())

    def test_positive_price_keeps_existing_wallet_charge(self):
        self.model.input_token_rate_per_million = Decimal("1.00")
        self.model.save(update_fields=["input_token_rate_per_million"])
        before = self.wallet.balance
        self.service.finalize_ai_message(
            self.message, "READY", {"input_tokens": 1000000, "output_tokens": 0}
        )
        row = self.usage().get()
        self.assertEqual(row.amount, Decimal("1.00"))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, before - Decimal("1.00"))

    def test_recorded_usage_appears_in_dashboard_and_model_stats(self):
        self.service.finalize_ai_message(
            self.message, "READY", {"input_tokens": 22, "output_tokens": 35}
        )
        client = APIClient()
        client.force_authenticate(self.user)
        stats = client.get("/users/api/stats/").json()
        self.assertEqual(stats["totalInputTokens"], 22)
        self.assertEqual(stats["totalOutputTokens"], 35)
        rows = client.get("/api/billing/model_stats/").json()["modelsBillingStats"]
        row = next(row for row in rows if row["llmName"] == self.model.name)
        self.assertEqual(row["transactionCount"], 1)
        transactions = client.get("/api/billing/transactions/").json()["results"]
        record = next(item for item in transactions if item["id"] == self.usage().get().id)
        self.assertEqual((record["inputTokens"], record["outputTokens"]), (22, 35))

    def test_zero_cost_does_not_create_a_missing_wallet(self):
        self.wallet.delete()
        self.service.finalize_ai_message(
            self.message, "READY", {"input_tokens": 22, "output_tokens": 35}
        )
        self.assertFalse(Wallet.objects.filter(user=self.user).exists())
        self.assertEqual(self.usage().count(), 1)


@skipUnless(connection.vendor == "postgresql", "requires PostgreSQL row locking")
class ConcurrentZeroCostUsageTests(TransactionTestCase):
    def test_concurrent_replay_creates_one_transaction(self):
        user = User.objects.create_user(email="concurrent-zero@example.test", password="test")
        model = LLM.objects.create(name="Concurrent free", identifier="concurrent-free-test", provider="custom", input_token_rate_per_million=0, output_token_rate_per_million=0)
        conversation = Conversation.active_objects.create(user=user)
        message = Message.active_objects.create(conversation=conversation, sender_type=2, llm=model, message="")
        attempt_id = MessageUsageAttempt.objects.create(message=message).pk
        barrier = Barrier(2)

        def finalize():
            close_old_connections()
            try:
                current = Message.active_objects.get(pk=message.pk)
                barrier.wait(timeout=10)
                BillingService().finalize_ai_message(current, "ready", {"input_tokens": 22, "output_tokens": 35}, usage_attempt_id=attempt_id)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: finalize(), range(2)))
        rows = Transaction.objects.filter(user=user, source=TransactionSourceChoice.USAGE)
        self.assertEqual(rows.count(), 1)
        self.assertEqual((rows.get().input_tokens, rows.get().output_tokens), (22, 35))

