"""Google OAuth application credentials for Bird Buddy sign-in."""

from homeassistant.components.application_credentials import AuthorizationServer
from homeassistant.core import HomeAssistant


async def async_get_authorization_server(hass: HomeAssistant) -> AuthorizationServer:
    """Return Google's authorization-code endpoints."""
    return AuthorizationServer(
        "https://accounts.google.com/o/oauth2/v2/auth",
        "https://oauth2.googleapis.com/token",
    )


async def async_get_description_placeholders(hass: HomeAssistant) -> dict[str, str]:
    """Link the setup dialog to Google Cloud's credential configuration."""
    return {"console_url": "https://console.cloud.google.com/auth/clients"}
