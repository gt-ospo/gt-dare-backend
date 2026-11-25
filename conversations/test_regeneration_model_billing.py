from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from django.test import TransactionTestCase

from api_keys.constants import BillingModeChoice
from billing.constants import LiteLLMKeySourceChoice, TransactionSourceChoice
from billing.models import LiteLLMKey, Transaction, Wallet
from conversations.constants import SenderType
from conversations.models import Conversation, LLM, Message
from conversations.services.message_coordinator import MessageCoordinator
from core.services.dtos import LLMDescriptor
from users.models import User


class RegenerationModelBillingTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="regenerate@example.test", password="test"
        )
        self.wallet, _ = Wallet.objects.update_or_create(
            user=self.user, defaults={"balance": Decimal("10")}
        )
        self.free = LLM.objects.create(
            name="free",
            identifier="free-test",
            provider="openai",
            input_token_rate_per_million=Decimal("0"),
            output_token_rate_per_million=Decimal("0"),
        )
        self.paid = LLM.objects.create(
            name="paid",
            identifier="paid-test",
            provider="openai",
            input_token_rate_per_million=Decimal("1000"),
            output_token_rate_per_million=Decimal("1000"),
        )
        self.conversation = Conversation.active_objects.create(user=self.user)
        self.key = LiteLLMKey.objects.create(
            label="personal",
            base_url="https://proxy.example/v1",
            api_key="test-only",
            source=LiteLLMKeySourceChoice.USER,
            owner_user=self.user,
            created_by=self.user,
        )
        self.previous = Message.active_objects.create(
            conversation=self.conversation,
            sender_type=SenderType.AI_ASSISTANT,
            llm=self.free,
            message="previous",
        )

    def regenerate(self, descriptor, *, enough_credit=True):
        coordinator = MessageCoordinator(self.conversation, user=self.user)
        coordinator._get_preceding_user_message = AsyncMock(
            return_value=SimpleNamespace(message="question")
        )
        coordinator._ensemble_for_turn = AsyncMock(return_value=None)
        coordinator._get_descriptor = AsyncMock(return_value=descriptor)
        coordinator._prepare_regeneration_data = AsyncMock(
            return_value=(descriptor.to_dispatch_handle(), {"message": "question"})
        )
        coordinator.billing_service.check_sufficient_credits = AsyncMock(
            return_value=enough_credit
        )
        coordinator._clear_regeneration_run_state = AsyncMock()
        coordinator._send_regeneration_placeholder = AsyncMock()
        coordinator.send_error = AsyncMock()

        async def finish(*, message_obj, **kwargs):
            await database_sync_to_async(
                coordinator.billing_service.finalize_ai_message
            )(message_obj, "new response", {"input_tokens": 10, "output_tokens": 10})

        coordinator._run_generation = AsyncMock(side_effect=finish)
        with patch(
            "core.services.turn_dispatch_snapshot.is_flag_enabled_for_user",
            return_value=True,
        ):
            result = async_to_sync(coordinator.handle_regenerate_response)(
                {"message_id": self.previous.pk}, model_id="selected"
            )
        return coordinator, result

    def transactions(self):
        return Transaction.objects.filter(
            user=self.user, source=TransactionSourceChoice.USAGE
        )

    def test_free_to_paid_regeneration_charges_selected_model(self):
        _, result = self.regenerate(LLMDescriptor.from_llm(self.paid))
        self.assertIsNotNone(result)
        row = self.transactions().get()
        self.assertEqual(row.llm_id, self.paid.pk)
        self.assertEqual(row.amount, Decimal("0.02"))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal("9.98"))
        self.previous.refresh_from_db()
        self.assertEqual(self.previous.llm_id, self.paid.pk)

    def test_paid_to_free_regeneration_records_usage_without_charge(self):
        self.previous.llm = self.paid
        self.previous.save(update_fields=["llm"])
        self.regenerate(LLMDescriptor.from_llm(self.free))
        row = self.transactions().get()
        self.assertEqual((row.llm_id, row.amount), (self.free.pk, Decimal("0")))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal("10"))
        self.previous.refresh_from_db()
        self.assertEqual(self.previous.llm_id, self.free.pk)

    def test_wallet_to_proxy_and_back_use_selected_connection(self):
        self.regenerate(LLMDescriptor.from_litellm(self.key, "spin-test", "custom"))
        proxy = self.transactions().get()
        self.assertEqual(
            (proxy.billing_mode, proxy.llm_name),
            (BillingModeChoice.LITELLM, "spin-test"),
        )
        self.previous.refresh_from_db()
        self.assertEqual(
            (
                self.previous.llm_id,
                self.previous.litellm_key_id,
                self.previous.litellm_model_name,
            ),
            (None, self.key.pk, "spin-test"),
        )
        self.regenerate(LLMDescriptor.from_llm(self.paid))
        self.previous.refresh_from_db()
        self.assertEqual(
            (
                self.previous.llm_id,
                self.previous.litellm_key_id,
                self.previous.litellm_model_name,
            ),
            (self.paid.pk, None, None),
        )
        self.assertEqual(self.transactions().count(), 2)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal("9.98"))

    def test_insufficient_credit_keeps_original_provenance(self):
        coordinator, result = self.regenerate(
            LLMDescriptor.from_llm(self.paid), enough_credit=False
        )
        self.assertIsNone(result)
        self.previous.refresh_from_db()
        self.assertEqual(
            (self.previous.llm_id, self.previous.message), (self.free.pk, "previous")
        )
        coordinator._run_generation.assert_not_awaited()
        self.assertFalse(self.transactions().exists())

    def test_distinct_loaded_instances_keep_independent_billing_identity(self):
        first = Message.active_objects.get(pk=self.previous.pk)
        second = Message.active_objects.get(pk=self.previous.pk)
        first.llm = self.paid
        second.llm = self.free
        service = MessageCoordinator(self.conversation, user=self.user).billing_service
        service.finalize_ai_message(
            first, "paid result", {"input_tokens": 10, "output_tokens": 10}
        )
        service.finalize_ai_message(
            second, "free result", {"input_tokens": 10, "output_tokens": 10}
        )
        rows = list(self.transactions().order_by("pk"))
        self.assertEqual(
            [(row.llm_id, row.amount) for row in rows],
            [(self.paid.pk, Decimal("0.02")), (self.free.pk, Decimal("0"))],
        )

    def test_bot_and_audio_regeneration_leave_legacy_model_fields_unchanged(self):
        audio = LLM.objects.create(
            name="audio",
            identifier="audio-test",
            provider="openai",
            is_audio_transcriber=True,
        )
        for bot_id, selected, original in (
            (17, self.paid, self.free),
            (None, audio, self.free),
            (None, self.paid, audio),
        ):
            with self.subTest(
                bot_id=bot_id,
                selected=selected.identifier,
                original=original.identifier,
            ):
                self.previous.llm = original
                self.previous.save(update_fields=["llm"])
                conversation = SimpleNamespace(
                    id=self.conversation.pk,
                    source=self.conversation.source,
                    bot_id=bot_id,
                )
                coordinator = MessageCoordinator(conversation, user=self.user)
                coordinator._get_preceding_user_message = AsyncMock(
                    return_value=SimpleNamespace(message="question")
                )
                coordinator._ensemble_for_turn = AsyncMock(return_value=None)
                coordinator._get_descriptor = AsyncMock(
                    return_value=LLMDescriptor.from_llm(selected)
                )
                coordinator._prepare_regeneration_data = AsyncMock(
                    return_value=(selected, {"message": "question"})
                )
                coordinator.billing_service.check_sufficient_credits = AsyncMock(
                    return_value=True
                )
                coordinator._clear_regeneration_run_state = AsyncMock()
                coordinator._send_regeneration_placeholder = AsyncMock()
                coordinator._run_generation = AsyncMock()
                async_to_sync(coordinator.handle_regenerate_response)(
                    {"message_id": self.previous.pk}, model_id="selected"
                )
                passed_message = coordinator._run_generation.await_args.kwargs[
                    "message_obj"
                ]
                self.assertEqual(passed_message.llm_id, original.pk)
                self.assertFalse(hasattr(passed_message, "_dispatch_snapshot"))
