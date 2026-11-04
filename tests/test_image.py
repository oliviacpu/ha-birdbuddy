"""Tests for the Bird Buddy recent-visitor image entity."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from birdbuddy.media import Media
import pytest

from custom_components.birdbuddy.const import DOMAIN
from custom_components.birdbuddy.device import BirdBuddyDevice
from custom_components.birdbuddy.image import BirdBuddyRecentVisitorImageEntity

_EXPIRED = "https://example.invalid/e.jpg?Expires=1"
_MEDIA = {
    "__typename": "MediaImage",
    "id": "m1",
    "createdAt": "2026-07-09T12:00:00.000Z",
    "contentUrl": "https://example.invalid/c.jpg",
    "thumbnailUrl": "https://example.invalid/t.jpg",
}


def _image_entity(hass):
    """Build a recent-visitor image entity for the test feeder.

    Args:
        hass: The Home Assistant instance.

    Returns:
        The image entity bound to a mock coordinator.
    """
    device = BirdBuddyDevice({"__typename": "FeederForOwner", "id": "f1", "name": "BB"})
    entity = BirdBuddyRecentVisitorImageEntity(hass, device, MagicMock())
    entity.hass = hass
    return entity


def test_image_returns_none(hass):
    """image() defers to URL-based delivery and returns no bytes."""
    assert _image_entity(hass).image() is None


async def test_load_image_forces_jpeg_content_type(hass):
    """A fetched image is retagged as image/jpeg for Home Assistant."""
    entity = _image_entity(hass)
    response = MagicMock()
    response.content = b"bytes"
    with patch.object(entity, "_fetch_url", AsyncMock(return_value=response)):
        image = await entity._async_load_image_from_url("https://x/y.jpg")
    assert image is not None
    assert image.content == b"bytes"
    assert image.content_type == "image/jpeg"


async def test_load_image_returns_none_without_response(hass):
    """An empty fetch yields no image."""
    entity = _image_entity(hass)
    with patch.object(entity, "_fetch_url", AsyncMock(return_value=None)):
        assert await entity._async_load_image_from_url("https://x") is None


def _visitors(media=_MEDIA):
    """Build a stand-in tracker whose latest visitor has the given media.

    Args:
        media: The latest media mapping, or None.

    Returns:
        A mock with ``latest_media`` set.
    """
    return MagicMock(latest_media=Media(media) if media else None)


def _response(content):
    """Build a fetch response carrying the given bytes.

    Args:
        content: The response body.

    Returns:
        A mock response.
    """
    return AsyncMock(return_value=MagicMock(content=content))


def _on_disk(hass):
    """List the photos saved for the test feeder.

    Args:
        hass: The Home Assistant instance.

    Returns:
        The names of the saved photos.
    """
    return sorted(
        p.name for p in Path(hass.config.path(".storage", DOMAIN, "f1")).glob("*")
    )


@pytest.fixture
def config_dir(hass, tmp_path):
    """Point Home Assistant's config directory at a temporary one."""
    hass.config.config_dir = str(tmp_path)


async def _refresh(entity, visitors):
    """Run an image refresh without writing state.

    Args:
        entity: The image entity.
        visitors: The tracker to refresh from.
    """
    with patch.object(entity, "async_write_ha_state"):
        await entity._async_refresh_image(visitors)


def test_on_recent_visitor_schedules_refresh(hass):
    """The recent-visitor listener hands the refresh to a background task."""
    entity = _image_entity(hass)
    refresh = MagicMock()
    create_task = MagicMock()
    with (
        patch.object(entity, "_async_refresh_image", refresh),
        patch.object(
            entity.coordinator.config_entry,
            "async_create_background_task",
            create_task,
        ),
    ):
        entity._on_recent_visitor(_visitors())
    refresh.assert_called_once()
    create_task.assert_called_once()


@pytest.mark.parametrize(
    ("known", "shown", "retries"),
    [(True, None, True), (True, "m1", False), (False, None, False)],
    ids=["not loaded yet", "already showing", "no visitor known"],
)
def test_poll_retries_photo_not_loaded_yet(hass, known, shown, retries):
    """Each poll retries only a known visitor whose photo isn't showing."""
    entity = _image_entity(hass)
    visitors = _visitors()
    entity.coordinator.visitors = {"f1": visitors} if known else {}
    entity._shown_media_id = shown
    retry = MagicMock()
    with (
        patch.object(entity, "_on_recent_visitor", retry),
        patch.object(entity, "async_write_ha_state"),
    ):
        entity._handle_coordinator_update()

    if retries:
        retry.assert_called_once_with(visitors)
    else:
        retry.assert_not_called()


@pytest.mark.usefixtures("config_dir")
async def test_refresh_downloads_and_saves_photo(hass):
    """A new visitor's photo is fetched at once, saved and served locally."""
    entity = _image_entity(hass)
    with patch.object(entity, "_fetch_url", _response(b"photo")) as fetch:
        await _refresh(entity, _visitors())

    fetch.assert_awaited_once_with("https://example.invalid/c.jpg")
    assert await entity.async_image() == b"photo"
    assert entity.image_last_updated is not None
    assert _on_disk(hass) == ["m1.jpg"]
    # The signed URL expires, so viewers must go through the image proxy.
    assert entity.entity_picture is not None
    assert "example.invalid" not in entity.entity_picture


@pytest.mark.usefixtures("config_dir")
async def test_saved_photo_survives_restart_after_url_expires(hass):
    """A saved photo is served after a restart, without any fetch."""
    with patch.object(
        (first := _image_entity(hass)), "_fetch_url", _response(b"photo")
    ):
        await _refresh(first, _visitors())

    restarted = _image_entity(hass)
    with patch.object(restarted, "_fetch_url", AsyncMock()) as fetch:
        await _refresh(restarted, _visitors(_MEDIA | {"contentUrl": _EXPIRED}))

    fetch.assert_not_awaited()
    assert await restarted.async_image() == b"photo"
    assert restarted.image_last_updated is not None


@pytest.mark.usefixtures("config_dir")
async def test_same_visitor_is_not_fetched_again(hass):
    """A replay of the photo already showing does nothing."""
    entity = _image_entity(hass)
    with patch.object(entity, "_fetch_url", _response(b"photo")) as fetch:
        await _refresh(entity, _visitors())
        await _refresh(entity, _visitors())
    fetch.assert_awaited_once()


@pytest.mark.usefixtures("config_dir")
async def test_failed_download_keeps_current_photo(hass):
    """A photo that can't be fetched leaves the previous one showing."""
    entity = _image_entity(hass)
    with patch.object(entity, "_fetch_url", _response(b"old")):
        await _refresh(entity, _visitors())
    shown_at = entity.image_last_updated

    newer = _MEDIA | {"id": "m2", "createdAt": "2026-07-10T12:00:00.000Z"}
    with patch.object(entity, "_fetch_url", AsyncMock(return_value=None)):
        await _refresh(entity, _visitors(newer))

    assert await entity.async_image() == b"old"
    assert entity.image_last_updated == shown_at
    assert _on_disk(hass) == ["m1.jpg"]


@pytest.mark.usefixtures("config_dir")
@pytest.mark.parametrize(
    "latest",
    [_MEDIA | {"id": "m2", "contentUrl": _EXPIRED}, None],
    ids=["expired URL", "no media"],
)
async def test_saved_photo_shows_when_latest_cannot_load(hass, latest):
    """After a restart, an older saved photo beats showing nothing."""
    first = _image_entity(hass)
    with patch.object(first, "_fetch_url", _response(b"old")):
        await _refresh(first, _visitors())

    restarted = _image_entity(hass)
    await _refresh(restarted, _visitors(latest))

    assert await restarted.async_image() == b"old"
    assert restarted.image_last_updated is not None
    assert _on_disk(hass) == ["m1.jpg"]


@pytest.mark.usefixtures("config_dir")
async def test_latest_replaces_saved_photo_once_it_can_load(hass):
    """The fallback photo gives way when the latest visitor's loads."""
    first = _image_entity(hass)
    with patch.object(first, "_fetch_url", _response(b"old")):
        await _refresh(first, _visitors())

    restarted = _image_entity(hass)
    expired = _MEDIA | {"id": "m2", "contentUrl": _EXPIRED}
    await _refresh(restarted, _visitors(expired))
    with patch.object(restarted, "_fetch_url", _response(b"new")):
        await _refresh(restarted, _visitors(_MEDIA | {"id": "m2"}))

    assert await restarted.async_image() == b"new"
    assert _on_disk(hass) == ["m2.jpg"]


@pytest.mark.usefixtures("config_dir")
async def test_new_photo_replaces_saved_one(hass):
    """Only the latest photo stays on disk."""
    entity = _image_entity(hass)
    with patch.object(entity, "_fetch_url", _response(b"old")):
        await _refresh(entity, _visitors())
    with patch.object(entity, "_fetch_url", _response(b"new")):
        await _refresh(entity, _visitors(_MEDIA | {"id": "m2"}))

    assert await entity.async_image() == b"new"
    assert _on_disk(hass) == ["m2.jpg"]


@pytest.mark.usefixtures("config_dir")
async def test_expired_url_without_saved_photo_shows_nothing(hass):
    """An expired URL with nothing on disk leaves the entity empty."""
    entity = _image_entity(hass)
    with patch.object(entity, "_fetch_url", AsyncMock()) as fetch:
        await _refresh(entity, _visitors(_MEDIA | {"contentUrl": _EXPIRED}))

    fetch.assert_not_awaited()
    assert entity.image_last_updated is None


async def test_no_media_keeps_current_photo(hass):
    """A visitor with no media doesn't clear what's showing."""
    entity = _image_entity(hass)
    await _refresh(entity, _visitors(None))
    assert entity.image_last_updated is None


@pytest.mark.usefixtures("config_dir")
async def test_video_uses_its_thumbnail(hass):
    """A video's content URL isn't an image, so its thumbnail is used."""
    entity = _image_entity(hass)
    with patch.object(entity, "_fetch_url", _response(b"thumb")) as fetch:
        await _refresh(entity, _visitors(_MEDIA | {"__typename": "MediaVideo"}))
    fetch.assert_awaited_once_with("https://example.invalid/t.jpg")


@pytest.mark.usefixtures("config_dir")
async def test_unwritable_disk_still_shows_photo(hass):
    """A failed save is logged, and the photo still shows."""
    entity = _image_entity(hass)
    with (
        patch.object(entity, "_fetch_url", _response(b"photo")),
        patch("pathlib.Path.write_bytes", side_effect=OSError("disk full")),
    ):
        await _refresh(entity, _visitors())
    assert await entity.async_image() == b"photo"
