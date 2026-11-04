"""The Bird Buddy image entity."""

import asyncio
from pathlib import Path

from birdbuddy.media import Media, is_media_expired
from homeassistant.components.image import Image, ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.storage import STORAGE_DIR
from homeassistant.util import slugify

from .const import DOMAIN, LOGGER
from .coordinator import BirdBuddyDataUpdateCoordinator
from .device import BirdBuddyDevice
from .entity import BirdBuddyMixin
from .visitors import RecentVisitors


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Bird Buddy image entities from a config entry.

    Args:
        hass: The Home Assistant instance.
        entry: The config entry being set up.
        async_add_entities: Callback used to register the new entities.
    """
    coordinator: BirdBuddyDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    feeders = coordinator.feeders.values()
    async_add_entities(
        BirdBuddyRecentVisitorImageEntity(hass, f, coordinator) for f in feeders
    )


def _read_cached_image(path: Path) -> bytes | None:
    """Read a saved photo from disk.

    Args:
        path: The photo's path.

    Returns:
        The photo's bytes, or None if it can't be read.
    """
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as err:
        LOGGER.warning("Could not read saved visitor photo %s: %s", path, err)
        return None


def _write_cached_image(path: Path, content: bytes) -> None:
    """Save a photo to disk and remove the one it replaces.

    Args:
        path: The photo's path.
        content: The photo's bytes.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_bytes(content)
        temp.replace(path)
        for old in path.parent.glob("*.jpg"):
            if old != path:
                old.unlink(missing_ok=True)
    except OSError as err:
        LOGGER.warning("Could not save visitor photo %s: %s", path, err)


class BirdBuddyRecentVisitorImageEntity(BirdBuddyMixin, ImageEntity):
    """The latest visitor image entity.

    Media URLs are signed and expire after a day, and postcards leave the feed
    within days, so each photo is downloaded as soon as it's known and kept on
    disk. The entity always serves those bytes, through Home Assistant's image
    proxy, rather than handing out the signed URL.
    """

    _attr_has_entity_name = True
    _attr_name = "Recent Visitor Image"

    _latest_media: Media | None = None

    def __init__(
        self,
        hass: HomeAssistant,
        feeder: BirdBuddyDevice,
        coordinator: BirdBuddyDataUpdateCoordinator,
    ) -> None:
        """Initialize the recent-visitor image entity.

        Args:
            hass: The Home Assistant instance.
            feeder: The Bird Buddy device this entity represents.
            coordinator: The coordinator providing feeder updates.
        """
        ImageEntity.__init__(self, hass)
        BirdBuddyMixin.__init__(self, feeder, coordinator)
        self._latest_media = None
        self._shown_media_id: str | None = None
        self._refresh_lock = asyncio.Lock()
        self._attr_unique_id = f"{self.feeder.id}-recent-image"

    def image(self) -> bytes | None:
        """Return the image bytes.

        Returns:
            None; image data is served from ``_cached_image``.
        """
        # See _async_refresh_image()
        return None

    async def _async_load_image_from_url(self, url: str) -> Image | None:
        """Load an image by URL, ensuring compatibility with Home Assistant.

        This method overrides the parent implementation because cloudfront
        sometimes returns a `text/plain` content type for image data, which
        is incompatible with Home Assistant's requirement for `image/*`.
        To address this, the content type is explicitly set to `image/jpeg`.

        Args:
            url: The image URL to fetch.

        Returns:
            The fetched image tagged as `image/jpeg`, or None if the fetch
            returned no content.

        Raises:
            HomeAssistantError: If ``_fetch_url`` encounters an HTTP error.
        """
        if response := await self._fetch_url(url):
            return Image(
                content=response.content,
                content_type="image/jpeg",
            )
        return None

    async def async_added_to_hass(self) -> None:
        """Register the recent-visitor listener when added to hass."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.add_visitor_listener(
                self.feeder,
                self._on_recent_visitor,
            )
        )

    @callback
    def _on_recent_visitor(self, visitors: RecentVisitors) -> None:
        """Show the latest recorded visitor's photo.

        Args:
            visitors: The recent-visitors tracker for this feeder.
        """
        self.coordinator.config_entry.async_create_background_task(
            self.hass,
            self._async_refresh_image(visitors),
            f"{DOMAIN} recent visitor image {self.feeder.id}",
        )

    async def _async_refresh_image(self, visitors: RecentVisitors) -> None:
        """Swap in the latest visitor's photo once its bytes are in hand.

        A photo that can't be loaded leaves the current one showing. The lock
        keeps overlapping updates in order, and each reads the visitor only
        once it holds the lock, so the newest always wins.

        Args:
            visitors: The recent-visitors tracker for this feeder.
        """
        async with self._refresh_lock:
            media = visitors.latest_media
            if media is None or media.id == self._shown_media_id:
                return
            if (content := await self._async_load_media(media)) is None:
                return
            self._cached_image = Image(content_type="image/jpeg", content=content)
            self._attr_image_last_updated = media.created_at
            self._shown_media_id = media.id
            self.async_write_ha_state()

    async def _async_load_media(self, media: Media) -> bytes | None:
        """Load a photo from disk, or download and save it.

        Args:
            media: The media to load.

        Returns:
            The photo's bytes, or None if it isn't saved and can't be fetched.
        """
        path = Path(
            self.hass.config.path(STORAGE_DIR, DOMAIN, slugify(self.feeder.id))
        ) / (f"{slugify(media.id)}.jpg")
        if content := await self.hass.async_add_executor_job(_read_cached_image, path):
            return content

        # A video's content URL isn't an image; its thumbnail is.
        url = (
            media.thumbnail_url
            if media.is_video
            else media.content_url or media.thumbnail_url
        )
        if not url or is_media_expired(url):
            LOGGER.debug("No usable photo URL for %s", self.feeder.name)
            return None
        if (image := await self._async_load_image_from_url(url)) is None:
            return None
        await self.hass.async_add_executor_job(_write_cached_image, path, image.content)
        return image.content
