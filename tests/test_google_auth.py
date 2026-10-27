"""Google setup, identity checks, and persistent-session regression coverage."""

import json
from unittest.mock import AsyncMock, PropertyMock, patch

from birdbuddy.auth import AuthTokens
from birdbuddy.client import BirdBuddy
from birdbuddy.exceptions import AuthenticationFailedError
from birdbuddy.user import BirdBuddyUser
from homeassistant import config_entries
from homeassistant.data_entry_flow import AbortFlow, FlowResultType
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.birdbuddy import async_migrate_entry, async_setup_entry
from custom_components.birdbuddy.config_flow import ConfigFlow
from custom_components.birdbuddy.const import DOMAIN
from custom_components.birdbuddy.coordinator import BirdBuddyDataUpdateCoordinator
from custom_components.birdbuddy.diagnostics import async_get_config_entry_diagnostics

_USER = BirdBuddyUser(
    {"id": "bird-user", "email": "bird@example.com", "name": "Bird watcher"}
)
_GOOGLE_DATA = {
    "auth_method": "google",
    "email": "bird@example.com",
    "user_id": "bird-user",
    "refresh_token": "saved-refresh",
}


@pytest.fixture
def google_flow(hass):
    """Create a flow with no external service dependencies."""
    flow = ConfigFlow()
    flow.hass = hass
    flow.handler = DOMAIN
    flow.context = {"source": config_entries.SOURCE_USER}
    flow.flow_id = "google-test-flow"
    return flow


async def test_google_entry_drops_google_tokens(google_flow, caplog):
    """Only the Bird Buddy refresh token is saved in the configuration entry."""
    google = {
        "access_token": "google-access-secret",
        "refresh_token": "google-refresh-secret",
    }
    with (
        patch.object(BirdBuddy, "sign_in_google", return_value=True) as signin,
        patch.object(BirdBuddy, "user", new_callable=PropertyMock, return_value=_USER),
        patch.object(
            BirdBuddy,
            "tokens",
            new_callable=PropertyMock,
            return_value=AuthTokens("bb-access", "bb-refresh"),
        ),
    ):
        result = await google_flow.async_oauth_create_entry(
            {"auth_implementation": "google-client", "token": google}
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        **_GOOGLE_DATA,
        "refresh_token": "bb-refresh",
        "auth_implementation": "google-client",
    }
    signin.assert_awaited_once_with("google-access-secret")
    assert google == {}
    assert "google-access-secret" not in json.dumps(result)
    assert "google-refresh-secret" not in caplog.text


@pytest.mark.parametrize(
    ("exception", "reason"),
    [(AuthenticationFailedError(), "invalid_auth"), (TimeoutError(), "cannot_connect")],
)
async def test_google_failures_can_restart_authorization(
    google_flow, exception, reason
):
    """Rejected credentials and outages display an actionable Google retry form."""
    google = {"access_token": "google-access-secret"}
    with patch.object(BirdBuddy, "sign_in_google", side_effect=exception):
        result = await google_flow.async_oauth_create_entry(
            {"auth_implementation": "google-client", "token": google}
        )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "google"
    assert result["errors"] == {"base": reason}
    assert google == {}


async def test_callback_data_cleared_after_exchange(google_flow):
    """The authorization code is cleared after resolving Google's callback."""
    google_flow.external_data = {"code": "one-time-code"}
    google_flow.flow_impl = AsyncMock()
    google_flow.flow_impl.domain = "google-client"
    google_flow.flow_impl.async_resolve_external_data.return_value = {
        "access_token": "google-secret"
    }
    with patch.object(
        BirdBuddy, "sign_in_google", side_effect=AuthenticationFailedError
    ):
        await google_flow.async_step_creation()
    assert google_flow.external_data is None


async def test_cancelled_google_authorization(google_flow):
    """Cancelling Google consent ends the flow without creating an entry."""
    step = await google_flow.async_step_auth({"error": "access_denied"})
    assert step["type"] == FlowResultType.EXTERNAL_STEP_DONE
    result = await google_flow.async_step_authorize_rejected()
    assert result["reason"] == "user_rejected_authorize"


async def test_duplicate_google_account_is_rejected(hass, google_flow):
    """An existing account cannot be added again under another method."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={"email": "bird@example.com"}, unique_id="bird@example.com"
    )
    entry.add_to_hass(hass)
    client = BirdBuddy()
    with (
        patch.object(BirdBuddy, "user", new_callable=PropertyMock, return_value=_USER),
        pytest.raises(AbortFlow) as error,
    ):
        await google_flow._async_finish(client, _GOOGLE_DATA.copy())
    assert error.value.reason == "already_configured"


@pytest.mark.parametrize("same_account", [True, False])
async def test_reauth_preserves_account_and_entry(hass, google_flow, same_account):
    """Renewal updates the same entry and rejects a different Bird Buddy owner."""
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, unique_id="bird@example.com", data=_GOOGLE_DATA.copy()
    )
    entry.add_to_hass(hass)
    google_flow.context = {
        "source": config_entries.SOURCE_REAUTH,
        "entry_id": entry.entry_id,
    }
    user = (
        _USER
        if same_account
        else BirdBuddyUser(
            {"id": "different-user", "email": "other@example.com", "name": "Other"}
        )
    )
    with (
        patch.object(BirdBuddy, "user", new_callable=PropertyMock, return_value=user),
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        result = await google_flow._async_finish(
            BirdBuddy(), {**_GOOGLE_DATA, "refresh_token": "renewed-refresh"}
        )
    assert result["reason"] == (
        "reauth_successful" if same_account else "wrong_account"
    )
    assert entry.data["refresh_token"] == (
        "renewed-refresh" if same_account else "saved-refresh"
    )
    assert entry.unique_id == "bird@example.com"


async def test_existing_password_entry_migration(hass):
    """Upgrading keeps password credentials, unique ID, and entry ID intact."""
    data = {"email": "bird@example.com", "password": "password-secret"}
    entry = MockConfigEntry(
        domain=DOMAIN, version=1, unique_id="bird@example.com", data=data
    )
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    assert entry.version == 2
    assert entry.data == {**data, "auth_method": "email"}
    assert entry.unique_id == "bird@example.com"


async def test_runtime_restores_and_saves_rotated_refresh_token(hass):
    """Runtime loads the stored session and saves rotation before platform setup."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data=_GOOGLE_DATA.copy(),
        state=config_entries.ConfigEntryState.SETUP_IN_PROGRESS,
    )
    entry.add_to_hass(hass)
    me = {
        "user": dict(_USER),
        "feeders": [{"__typename": "FeederForOwner", "id": "feeder1", "name": "BB"}],
    }
    responses = [
        {
            "data": {
                "authRefreshToken": {
                    "accessToken": "fresh-access",
                    "refreshToken": "rotated-refresh",
                }
            }
        },
        {"data": {"me": me}},
    ]
    with (
        patch(
            "python_graphql_client.graphql_client.GraphqlClient.execute_async",
            side_effect=responses,
        ) as graphql,
        patch.object(
            hass.config_entries, "async_forward_entry_setups", new_callable=AsyncMock
        ),
    ):
        assert await async_setup_entry(hass, entry)
    assert graphql.call_args_list[0].kwargs["variables"] == {
        "refreshTokenInput": {"token": "saved-refresh"}
    }
    assert entry.data["refresh_token"] == "rotated-refresh"
    assert "fresh-access" not in json.dumps(dict(entry.data))


@pytest.mark.parametrize("expired", [True, False])
async def test_runtime_distinguishes_reauthentication_from_retry(hass, expired):
    """Only permanent authentication failure requires Google sign-in again."""
    entry = MockConfigEntry(domain=DOMAIN, version=2, data=_GOOGLE_DATA.copy())
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.refresh.side_effect = (
        AuthenticationFailedError() if expired else TimeoutError()
    )
    coordinator = BirdBuddyDataUpdateCoordinator(hass, client, entry)
    with pytest.raises(ConfigEntryAuthFailed if expired else UpdateFailed):
        await coordinator._async_update_data()


async def test_diagnostics_omit_all_credentials(hass):
    """Diagnostics never expose refresh tokens, passwords, or account identity."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={**_GOOGLE_DATA, "password": "password-secret", "token": "google-secret"},
    )
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result == {"auth_method": "google", "config_entry_version": 2}


async def test_password_duplicate_is_not_reported_as_connection_error(
    hass, google_flow
):
    """Password duplicate detection reaches the normal already-configured result."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={"email": "bird@example.com"}, unique_id="bird@example.com"
    )
    entry.add_to_hass(hass)
    with (
        patch.object(BirdBuddy, "refresh", return_value=True),
        patch.object(BirdBuddy, "user", new_callable=PropertyMock, return_value=_USER),
        pytest.raises(AbortFlow) as error,
    ):
        await google_flow.async_step_email(
            {"email": "bird@example.com", "password": "password-secret"}
        )
    assert error.value.reason == "already_configured"
