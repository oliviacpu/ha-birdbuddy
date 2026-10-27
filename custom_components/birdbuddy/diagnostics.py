"""Credential-free Bird Buddy diagnostics."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import AUTH_EMAIL, CONF_AUTH_METHOD


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, str | int]:
    """Report authentication mode without exposing account data or tokens."""
    return {
        "auth_method": entry.data.get(CONF_AUTH_METHOD, AUTH_EMAIL),
        "config_entry_version": entry.version,
    }
