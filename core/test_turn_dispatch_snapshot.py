from decimal import Decimal
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.test import TestCase, TransactionTestCase

from api_keys.constants import BillingModeChoice
from api_keys.models import UserProviderAPIKey
from billing.constants import LiteLLMKeySourceChoice, UserWalletPreferenceTypeChoice
from billing.models import LiteLLMKey, Transaction, UserWalletPreference, Wallet
from conversations.constants import SenderType
from conversations.models import Conversation, LLM, Message
from conversations.services.message_coordinator import MessageCoordinator
from conversations.services.tool_loop_binding import (
    ChatBillingGate,
    ChatToolLoopBinding,
)
from core.services.billing_service import BillingService
from core.services.dtos import LLMDescriptor, ResolvedDispatchCredentials
from core.services.llm_service import LLMService
from core.services.turn_dispatch_snapshot import (
    TurnDispatchSnapshot,
    capture_turn_dispatch,
)
from users.models import User
from users.constants import AuthSourceChoice


class TurnDispatchSnapshotTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="snapshots@example.test", password="test"
        )
        self.other = User.objects.create_user(
            email="other@example.test", password="test"
        )
        self.wallet, _ = Wallet.objects.update_or_create(
            user=self.user, defaults={"balance": Decimal("10")}
        )
        self.user.refresh_from_db()
        self.key_a = self.key("selected", self.user, "secret-a")
        self.key_b = self.key("later", self.user, "secret-b")
        self.foreign = self.key("foreign", self.other, "secret-c")
        self.llm = LLM.objects.create(
            name="paid",
            identifier="paid-snapshot",
            provider="openai",
            input_token_rate_per_million=Decimal("1000"),
            output_token_rate_per_million=Decimal("1000"),
        )

    @staticmethod
    def key(label, user, secret):
        return LiteLLMKey.objects.create(
            label=label,
            base_url=f"https://{label}.example/v1",
            api_key=secret,
            source=LiteLLMKeySourceChoice.USER,
            owner_user=user,
            created_by=user,
        )

    def active_key(self, key):
        UserWalletPreference.objects.update_or_create(
            user=self.user,
            defaults={
                "active_wallet_type": UserWalletPreferenceTypeChoice.LITELLM,
                "active_wallet_ref_id": str(key.pk),
            },
        )

    def active_byo_without_openai_key(self):
        UserProviderAPIKey.active_objects.update_or_create(
            user=self.user, provider="claude", defaults={"api_key": "test-only"}
        )
        UserWalletPreference.get_or_create_for(self.user)
        UserWalletPreference.objects.filter(user=self.user).update(
            active_wallet_type=UserWalletPreferenceTypeChoice.BYO,
            active_wallet_ref_id=None,
        )
        self.user.billing_mode = BillingModeChoice.OWN_API
        self.user.save(update_fields=["billing_mode"])

    @patch("billing.wallet_router.is_flag_enabled_for_user", return_value=True)
    def test_byo_fallback_zero_usage_is_attributed_to_dare_wallet(self, _):
        self.active_byo_without_openai_key()
        self.llm.input_token_rate_per_million = Decimal("0")
        self.llm.output_token_rate_per_million = Decimal("0")
        self.llm.save(
            update_fields=[
                "input_token_rate_per_million",
                "output_token_rate_per_million",
            ]
        )
        snapshot = capture_turn_dispatch(self.user, LLMDescriptor.from_llm(self.llm))
        self.assertEqual(snapshot.wallet_type, UserWalletPreferenceTypeChoice.DARE)
        conversation = Conversation.active_objects.create(user=self.user)
        message = Message.active_objects.create(
            conversation=conversation, sender_type=SenderType.AI_ASSISTANT, llm=self.llm
        )
        message._dispatch_snapshot = snapshot
        BillingService().finalize_ai_message(
            message, "response", {"input_tokens": 10, "output_tokens": 10}
        )
        row = Transaction.objects.get(user=self.user, llm=self.llm)
        self.assertEqual(row.billing_mode, BillingModeChoice.WALLET)
        self.assertEqual(
            (row.input_tokens, row.output_tokens, row.amount), (10, 10, Decimal("0"))
        )
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal("10"))

    @patch(
        "core.services.turn_dispatch_snapshot.is_flag_enabled_for_user",
        return_value=True,
    )
    def test_selected_key_survives_later_wallet_change_for_dispatch_and_accounting(
        self, _
    ):
        self.active_key(self.key_a)
        snapshot = capture_turn_dispatch(
            self.user, LLMDescriptor.from_litellm(self.key_a, "model-a", "custom")
        )
        self.active_key(self.key_b)
        synthetic = LLMDescriptor.from_litellm(
            self.key_a, "model-a", "custom"
        ).to_dispatch_handle()
        with patch("core.services.llm_service.CustomLLMService") as transport:
            async_to_sync(LLMService()._get_ai_service)(synthetic, self.user, snapshot)
        self.assertEqual(transport.call_args.kwargs["api_key"], "secret-a")
        self.assertEqual(
            transport.call_args.kwargs["base_url"], "https://selected.example/v1"
        )
        self.assertNotIn("secret-a", repr(snapshot))
        conversation = Conversation.active_objects.create(user=self.user)
        message = Message.active_objects.create(
            conversation=conversation,
            sender_type=SenderType.AI_ASSISTANT,
            llm=None,
            litellm_key=self.key_a,
            litellm_model_name="model-a",
        )
        message._dispatch_snapshot = snapshot
        BillingService().finalize_ai_message(
            message, "response", {"input_tokens": 10, "output_tokens": 10}
        )
        transaction = Transaction.objects.get(
            user=self.user, message__contains="model=model-a"
        )
        self.assertIn("via selected", transaction.message)
        self.assertEqual(transaction.amount, Decimal("0"))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal("10"))

    @patch(
        "core.services.turn_dispatch_snapshot.is_flag_enabled_for_user",
        return_value=True,
    )
    def test_foreign_selected_key_is_rejected(self, _):
        with self.assertRaises(ValueError):
            capture_turn_dispatch(
                self.user, LLMDescriptor.from_litellm(self.foreign, "model-a", "custom")
            )

    @patch(
        "core.services.turn_dispatch_snapshot.is_flag_enabled_for_user",
        return_value=True,
    )
    def test_disabled_personal_connection_is_rejected(self, enabled):
        enabled.return_value = False
        with self.assertRaises(ValueError):
            capture_turn_dispatch(
                self.user, LLMDescriptor.from_litellm(self.key_a, "model-a", "custom")
            )

    @patch("billing.wallet_router.is_flag_enabled_for_user", return_value=True)
    def test_stale_direct_model_does_not_route_to_proxy(self, _):
        self.active_key(self.key_a)
        with self.assertRaises(ValueError):
            capture_turn_dispatch(self.user, LLMDescriptor.from_llm(self.llm))

    def test_byo_snapshot_does_not_debit_dare_wallet(self):
        conversation = Conversation.active_objects.create(user=self.user)
        message = Message.active_objects.create(
            conversation=conversation, sender_type=SenderType.AI_ASSISTANT, llm=self.llm
        )
        message._dispatch_snapshot = TurnDispatchSnapshot(
            ResolvedDispatchCredentials(
                api_key="test", wallet_type=UserWalletPreferenceTypeChoice.BYO
            )
        )
        BillingService().finalize_ai_message(
            message, "response", {"input_tokens": 10, "output_tokens": 10}
        )
        row = Transaction.objects.get(user=self.user, message__contains="Own API Key")
        self.assertEqual(row.amount, Decimal("0"))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal("10"))

    def test_external_snapshot_skips_dare_streaming_credit_gate(self):
        from unittest.mock import AsyncMock

        service = BillingService()
        service.check_streaming_credit_usage = AsyncMock(return_value=(False, {}))
        snapshot = TurnDispatchSnapshot(
            ResolvedDispatchCredentials(
                api_key="test", wallet_type=UserWalletPreferenceTypeChoice.BYO
            )
        )
        result = async_to_sync(
            ChatBillingGate(service, self.user, self.llm, snapshot).check
        )({"input_tokens": 20})
        self.assertEqual(result, (True, None))
        service.check_streaming_credit_usage.assert_not_awaited()

    def test_legacy_streaming_gate_keeps_existing_call_signature(self):
        from unittest.mock import AsyncMock

        service = BillingService()
        service.check_streaming_credit_usage = AsyncMock(return_value=(True, None))
        usage = {"input_tokens": 20}
        async_to_sync(ChatBillingGate(service, self.user, self.llm).check)(usage)
        service.check_streaming_credit_usage.assert_awaited_once_with(
            self.user, self.llm, usage
        )

    @patch("billing.wallet_router.is_flag_enabled_for_user", return_value=True)
    def test_dare_snapshot_does_not_switch_to_proxy_after_wallet_change(self, _):
        snapshot = capture_turn_dispatch(self.user, LLMDescriptor.from_llm(self.llm))
        self.active_key(self.key_b)
        with patch("core.services.llm_service.OpenAIService") as transport:
            async_to_sync(LLMService()._get_ai_service)(self.llm, self.user, snapshot)
        self.assertIsNone(transport.call_args.kwargs.get("base_url"))
        conversation = Conversation.active_objects.create(user=self.user)
        message = Message.active_objects.create(
            conversation=conversation, sender_type=SenderType.AI_ASSISTANT, llm=self.llm
        )
        message._dispatch_snapshot = snapshot
        BillingService().finalize_ai_message(
            message, "response", {"input_tokens": 10, "output_tokens": 10}
        )
        self.assertEqual(
            Transaction.objects.get(user=self.user, llm=self.llm).amount,
            Decimal("0.02"),
        )
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal("9.98"))


class PartialWalletChargeTests(TransactionTestCase):
    def cross_origin_case(self):
        user = User.objects.create_user(
            email="cross-origin@example.test", password="test"
        )
        wallet, _ = Wallet.objects.update_or_create(
            user=user, defaults={"balance": Decimal("0.01")}
        )
        user.refresh_from_db()
        user.auth_source = AuthSourceChoice.SOCRATIC_BOTS
        user.billing_mode = BillingModeChoice.OWN_API
        user.save(update_fields=["auth_source", "billing_mode"])
        model = LLM.objects.create(
            name="partial",
            identifier="partial-cross-origin",
            provider="openai",
            input_token_rate_per_million=Decimal("1000"),
            output_token_rate_per_million=Decimal("1000"),
        )
        conversation = Conversation.active_objects.create(
            user=user, source=AuthSourceChoice.DARE
        )
        message = Message.active_objects.create(
            conversation=conversation, sender_type=SenderType.AI_ASSISTANT, llm=model
        )
        message._dispatch_snapshot = TurnDispatchSnapshot(
            ResolvedDispatchCredentials(
                api_key="test-only", wallet_type=UserWalletPreferenceTypeChoice.DARE
            )
        )
        return user, wallet, model, conversation, message

    def assert_dare_wallet_debited(self, user, wallet):
        row = Transaction.objects.get(user=user, message__contains="Partial LLM usage")
        self.assertEqual(
            (row.amount, row.billing_mode, row.platform),
            (Decimal("0.01"), BillingModeChoice.WALLET, AuthSourceChoice.DARE),
        )
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal("0"))

    def test_tool_loop_partial_charge_uses_conversation_source(self):
        user, wallet, model, conversation, message = self.cross_origin_case()
        binding = ChatToolLoopBinding(
            message_obj=message,
            conversation=conversation,
            user=user,
            llm=model,
            send_callback=None,
            billing_service=BillingService(),
        )
        result = async_to_sync(binding.gate.check)(
            {"input_tokens": 100, "output_tokens": 100}
        )
        self.assertFalse(result[0])
        self.assert_dare_wallet_debited(user, wallet)

    def test_media_partial_charge_uses_conversation_source(self):
        from unittest.mock import AsyncMock

        user, wallet, model, conversation, message = self.cross_origin_case()
        coordinator = MessageCoordinator(conversation, user=user)
        coordinator._handle_insufficient_balance = AsyncMock()

        async def stream(_request):
            yield "", {"input_tokens": 100, "output_tokens": 100}

        coordinator.llm_service.query = stream
        async_to_sync(coordinator._stream_media_response)(
            object(), {}, message, model, False
        )
        coordinator._handle_insufficient_balance.assert_awaited_once()
        self.assert_dare_wallet_debited(user, wallet)

    @patch("billing.wallet_router.is_flag_enabled_for_user", return_value=True)
    def test_byo_fallback_partial_stream_uses_snapshot_after_wallet_switch(self, _):
        user = User.objects.create_user(email="partial@example.test", password="test")
        wallet, _ = Wallet.objects.update_or_create(
            user=user, defaults={"balance": Decimal("0.01")}
        )
        user.refresh_from_db()
        user.billing_mode = BillingModeChoice.OWN_API
        user.save(update_fields=["billing_mode"])
        UserProviderAPIKey.active_objects.update_or_create(
            user=user, provider="claude", defaults={"api_key": "test-only"}
        )
        UserWalletPreference.get_or_create_for(user)
        UserWalletPreference.objects.filter(user=user).update(
            active_wallet_type=UserWalletPreferenceTypeChoice.BYO,
            active_wallet_ref_id=None,
        )
        model = LLM.objects.create(
            name="partial",
            identifier="partial-test",
            provider="openai",
            input_token_rate_per_million=Decimal("1000"),
            output_token_rate_per_million=Decimal("1000"),
        )
        snapshot = capture_turn_dispatch(user, LLMDescriptor.from_llm(model))
        self.assertEqual(snapshot.wallet_type, UserWalletPreferenceTypeChoice.DARE)
        key = LiteLLMKey.objects.create(
            label="new choice",
            base_url="https://new.example/v1",
            api_key="test-only",
            source=LiteLLMKeySourceChoice.USER,
            owner_user=user,
            created_by=user,
        )
        UserWalletPreference.objects.filter(user=user).update(
            active_wallet_type=UserWalletPreferenceTypeChoice.LITELLM,
            active_wallet_ref_id=str(key.pk),
        )
        result = async_to_sync(
            ChatBillingGate(BillingService(), user, model, snapshot).check
        )({"input_tokens": 100, "output_tokens": 100})
        self.assertFalse(result[0])
        row = Transaction.objects.get(user=user, message__contains="Partial LLM usage")
        self.assertEqual(
            (row.amount, row.billing_mode), (Decimal("0.01"), BillingModeChoice.WALLET)
        )
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal("0"))
