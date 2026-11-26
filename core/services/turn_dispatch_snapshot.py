"""Capture a chat turn's authorized route once, without persisting secrets."""

from dataclasses import dataclass, field

from billing.constants import UserWalletPreferenceTypeChoice
from billing.models import LiteLLMKey
from core.services.api_key_service import get_dispatch_credentials_for_user_sync
from core.services.dtos import ResolvedDispatchCredentials
from feature_flags.services import is_flag_enabled_for_user


@dataclass(frozen=True)
class TurnDispatchSnapshot:
    credentials: ResolvedDispatchCredentials = field(repr=False)

    @property
    def wallet_type(self):
        return self.credentials.wallet_type


def capture_turn_dispatch(user, descriptor):
    """Bind the selected chat model to one currently authorized route.

    Credentials stay in memory for the lifetime of this turn. Explicit
    LiteLLM model identities use their selected key instead of consulting a
    wallet preference that may change before dispatch.
    """
    if descriptor.is_synthetic:
        if not is_flag_enabled_for_user(user, "enable_litellm_wallet"):
            raise ValueError(
                "The selected connection is unavailable. Choose another model."
            )
        key = (
            LiteLLMKey.visible_for_user(user)
            .filter(pk=descriptor.litellm_key.pk)
            .first()
        )
        if key is None or key.is_expired:
            raise ValueError(
                "The selected connection is unavailable. Choose another model."
            )
        return TurnDispatchSnapshot(
            ResolvedDispatchCredentials(
                api_key=key.api_key,
                base_url=key.base_url,
                wallet_type=UserWalletPreferenceTypeChoice.LITELLM,
            )
        )
    credentials = get_dispatch_credentials_for_user_sync(descriptor.provider, user)
    if credentials.use_litellm_proxy:
        raise ValueError(
            "Your wallet changed. Select a model for the active wallet and try again."
        )
    return TurnDispatchSnapshot(credentials)
