"""Bird Buddy Media Source.

Browsing goes account > species collection > visit > photos and video. Bird
Buddy's collections don't say which media came from the same visit, so the
media are grouped by time: a gap longer than ``VISIT_GAP`` starts a new visit.
"""

from datetime import datetime, timedelta
from typing import cast

from birdbuddy.media import Collection, Media
from homeassistant.components.media_player.const import MediaClass, MediaType
from homeassistant.components.media_source.error import MediaSourceError, Unresolvable
from homeassistant.components.media_source.models import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
import homeassistant.util.dt as dt_util

from .const import DOMAIN
from .coordinator import BirdBuddyDataUpdateCoordinator

# A visit's photos and video arrive within about 15 seconds; separate visits
# were at least two minutes apart (measured on real collections, 2026-11).
VISIT_GAP = timedelta(seconds=60)
# Third identifier part for a visit: VISIT_PREFIX + the id of its first media.
VISIT_PREFIX = "visit-"


class BirdBuddyMediaSource(MediaSource):
    """Provides bird collection previews as media sources."""

    name: str = "Bird Buddy"

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the media source.

        Args:
            hass: The Home Assistant instance.
        """
        super().__init__(DOMAIN)
        self.hass = hass

    def _root_media_source(self) -> BrowseMediaSource:
        """Build the root media source node.

        Returns:
            The root directory listing one node per configured account.
        """
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier="",
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title="Bird Buddy Media",
            can_play=False,
            can_expand=True,
            children=self._account_media_sources(),
            children_media_class=MediaClass.DIRECTORY,
        )

    @callback
    @classmethod
    def _parse_identifier(
        cls, identifier: str
    ) -> tuple[str | None, str | None, str | None]:
        """Split a media identifier into its component parts.

        Args:
            identifier: The ``#``-delimited media source identifier.

        Returns:
            A 3-tuple of (config id, collection id, media id); missing
            trailing components are None.
        """
        parts = identifier.split("#", 2)
        padded = [*parts, None, None, None]
        return cast(
            tuple[str | None, str | None, str | None],
            tuple(padded[:3]),
        )

    def _get_config_or_raise(self, config_id: str) -> ConfigEntry:
        """Return the config entry for an id.

        Args:
            config_id: The config entry id to resolve.

        Returns:
            The matching config entry.

        Raises:
            MediaSourceError: If no config entry with the id exists.
        """
        entry = self.hass.config_entries.async_get_entry(config_id)
        if not entry:
            msg = f"Unable to find config entry with id: {config_id}"
            raise MediaSourceError(msg)
        return entry

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Resolve a media item to a playable URL.

        Args:
            item: The media source item to resolve.

        Returns:
            The resolved playable media.

        Raises:
            Unresolvable: If the identifier is incomplete or the media has
                no content URL.
        """
        config_id, collection_id, media_id = self._parse_identifier(item.identifier)

        if (
            not config_id
            or not collection_id
            or not media_id
            or media_id.startswith(VISIT_PREFIX)
        ):
            msg = f"Incomplete media identifier specified: {item.identifier}"
            raise Unresolvable(msg)

        coordinator: BirdBuddyDataUpdateCoordinator = self.hass.data[DOMAIN][config_id]
        medias = await coordinator.client.collection(collection_id)
        media = medias[media_id]

        url = media.content_url
        if not url:
            msg = f"Could not resolve media item: {item.identifier}"
            raise Unresolvable(msg)

        return PlayMedia(url, _mime_type(media))

    async def async_browse_media(
        self,
        item: MediaSourceItem,
    ) -> BrowseMediaSource:
        """Browse the Bird Buddy media tree.

        Args:
            item: The media source item to browse.

        Returns:
            The browse result for the requested item, or the root listing
            when no identifier is supplied.
        """
        if item.identifier:
            config = None
            coordinator: BirdBuddyDataUpdateCoordinator | None = None
            config_id, collection_id, part = self._parse_identifier(item.identifier)
            if config_id:
                config = self._get_config_or_raise(config_id)
                coordinator = self.hass.data[DOMAIN][config_id]

            if coordinator and not coordinator.client.collections:
                await coordinator.client.refresh_collections()

            if config and collection_id and coordinator:
                if (
                    not coordinator.client.collections
                    or collection_id not in coordinator.client.collections
                ):
                    await coordinator.client.refresh_collections()
                collection = coordinator.client.collections[collection_id]
                if part and part.startswith(VISIT_PREFIX):
                    return await self._build_media_visit_entries(
                        config, coordinator, collection, part
                    )
                return await self._build_media_collection_entries(
                    config, coordinator, collection
                )

            if config and coordinator:
                return await self._build_media_collections(config, coordinator)

        # Root of the media source: show all configured logins
        return self._build_media_configs()

    def _account_media_source(self, config: ConfigEntry) -> BrowseMediaSource:
        """Build the media source node for one account.

        Args:
            config: The config entry (account) to represent.

        Returns:
            A directory node for the account's collections.
        """
        # Return one Bird Buddy account source per config entry
        coordinator: BirdBuddyDataUpdateCoordinator = self.hass.data[DOMAIN][
            config.entry_id
        ]
        user = coordinator.client.user
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=config.entry_id,
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title=config.title,
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.DIRECTORY,
            thumbnail=user.avatar_url if user else None,
        )

    def _build_media_config(self, config: ConfigEntry) -> BrowseMediaSource:
        """Build the media source for one configured account.

        Args:
            config: The config entry (account) to represent.

        Returns:
            The account directory node listing its feeders.
        """
        return self._account_media_source(config)

    def _build_media_configs(self) -> BrowseMediaSource:
        """Build the root media source for the whole integration.

        Returns:
            The root directory node.
        """
        return self._root_media_source()

    def _account_media_sources(self) -> list[BrowseMediaSource]:
        """Build one media source node per configured account.

        Returns:
            A node for each Bird Buddy config entry.
        """
        return [
            self._account_media_source(entry)
            for entry in self.hass.config_entries.async_entries(DOMAIN)
        ]

    @classmethod
    def _build_media_collection(
        cls,
        config: ConfigEntry,
        collection: Collection,
    ) -> BrowseMediaSource:
        """Build a media source node for one collection.

        Args:
            config: The config entry (account) the collection belongs to.
            collection: The bird collection to represent.

        Returns:
            An expandable directory node for the collection.
        """
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=f"{config.entry_id}#{collection.collection_id}",
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.IMAGE,
            title=collection.bird_name,
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.DIRECTORY,
            thumbnail=collection.cover_media.thumbnail_url,
        )

    async def _build_media_collection_entries(
        self,
        config: ConfigEntry,
        coordinator: BirdBuddyDataUpdateCoordinator,
        collection: Collection,
    ) -> BrowseMediaSource:
        """Build the visits of one collection, newest first.

        Args:
            config: The config entry (account) the collection belongs to.
            coordinator: The coordinator for the account.
            collection: The collection whose visits to list.

        Returns:
            The collection node populated with one folder per visit.
        """
        base = self._build_media_collection(config, collection)
        medias = await coordinator.client.collection(collection.collection_id)
        now = dt_util.utcnow()
        base.children = []
        for visit in reversed(_group_visits(medias)):
            first_id, first = visit[0]
            cover = next((m for _, m in visit if not m.is_video), first)
            base.children.append(
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=(
                        f"{config.entry_id}#{collection.collection_id}"
                        f"#{VISIT_PREFIX}{first_id}"
                    ),
                    media_class=MediaClass.DIRECTORY,
                    media_content_type=MediaType.IMAGE,
                    title=_best_timedelta_title(first.created_at, now),
                    can_play=False,
                    can_expand=True,
                    children_media_class=MediaClass.IMAGE,
                    thumbnail=cover.thumbnail_url,
                )
            )
        return base

    async def _build_media_visit_entries(
        self,
        config: ConfigEntry,
        coordinator: BirdBuddyDataUpdateCoordinator,
        collection: Collection,
        part: str,
    ) -> BrowseMediaSource:
        """Build the photos and video of one visit.

        Args:
            config: The config entry (account) the collection belongs to.
            coordinator: The coordinator for the account.
            collection: The collection the visit belongs to.
            part: The visit's identifier part (``VISIT_PREFIX`` + first media id).

        Returns:
            The visit node populated with its media.

        Raises:
            MediaSourceError: If the collection holds no such visit anymore.
        """
        medias = await coordinator.client.collection(collection.collection_id)
        first_id = part.removeprefix(VISIT_PREFIX)
        visit = next((v for v in _group_visits(medias) if v[0][0] == first_id), None)
        if visit is None:
            msg = f"Visit not found in {collection.bird_name}: {part}"
            raise MediaSourceError(msg)
        first = visit[0][1]
        base = BrowseMediaSource(
            domain=DOMAIN,
            identifier=f"{config.entry_id}#{collection.collection_id}#{part}",
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.IMAGE,
            title=_best_timedelta_title(first.created_at, dt_util.utcnow()),
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.IMAGE,
            thumbnail=first.thumbnail_url,
        )
        photos = [(i, m) for i, m in visit if not m.is_video]
        videos = [(i, m) for i, m in visit if m.is_video]
        base.children = [
            self._build_media_item(config, collection, media_id, media, title)
            for group, word in ((photos, "Photo"), (videos, "Video"))
            for media_id, media, title in _numbered(group, word)
        ]
        return base

    @staticmethod
    def _build_media_item(
        config: ConfigEntry,
        collection: Collection,
        media_id: str,
        media: Media,
        title: str,
    ) -> BrowseMediaSource:
        """Build the node for one photo or video.

        Args:
            config: The config entry (account) the media belongs to.
            collection: The collection the media belongs to.
            media_id: The media's id.
            media: The media.
            title: The node's title.

        Returns:
            A playable node for the media.
        """
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=f"{config.entry_id}#{collection.collection_id}#{media_id}",
            media_class=_media_class(media),
            media_content_type=_mime_type(media),
            title=title,
            can_play=media.is_video,
            can_expand=media.is_video,
            thumbnail=media.thumbnail_url,
        )

    async def _build_media_collections(
        self,
        config: ConfigEntry,
        coordinator: BirdBuddyDataUpdateCoordinator,
    ) -> BrowseMediaSource:
        """Build the collection nodes for one account.

        Args:
            config: The config entry (account) to list collections for.
            coordinator: The coordinator for the account.

        Returns:
            The account node populated with its collection children.
        """
        base = self._account_media_source(config)
        collections = await coordinator.client.refresh_collections()
        base.children = [
            self._build_media_collection(
                config,
                c,
            )
            for _, c in collections.items()
        ]
        return base


async def async_get_media_source(hass: HomeAssistant) -> BirdBuddyMediaSource:
    """Set up the Bird Buddy media source.

    Args:
        hass: The Home Assistant instance.

    Returns:
        The Bird Buddy media source.
    """
    return BirdBuddyMediaSource(hass)


def _group_visits(medias: dict[str, Media]) -> list[list[tuple[str, Media]]]:
    """Group a collection's media into visits by time.

    Args:
        medias: The collection's media, by id.

    Returns:
        The visits, oldest first; each is its (id, media) pairs, oldest first.
    """
    visits: list[list[tuple[str, Media]]] = []
    last: datetime | None = None
    for media_id, media in sorted(medias.items(), key=lambda i: i[1].created_at):
        if last is None or media.created_at - last > VISIT_GAP:
            visits.append([])
        visits[-1].append((media_id, media))
        last = media.created_at
    return visits


def _numbered(
    group: list[tuple[str, Media]], word: str
) -> list[tuple[str, Media, str]]:
    """Title each media of one kind: "Photo" alone, or "Photo 1", "Photo 2"...

    Args:
        group: The (id, media) pairs of one kind.
        word: The kind's name.

    Returns:
        (id, media, title) for each.
    """
    if len(group) == 1:
        return [(group[0][0], group[0][1], word)]
    return [(i, m, f"{word} {n}") for n, (i, m) in enumerate(group, 1)]


def _media_class(media: Media) -> MediaClass:
    """Return the media class for a media item.

    Args:
        media: The media item to classify.

    Returns:
        ``MediaClass.VIDEO`` for videos, otherwise ``MediaClass.IMAGE``.
    """
    if media.get("__typename") == "MediaVideo":
        return MediaClass.VIDEO
    return MediaClass.IMAGE


def _mime_type(media: Media) -> str:
    """Return the MIME type for a media item.

    Args:
        media: The media item to inspect.

    Returns:
        ``video/mp4`` for videos, otherwise ``image/jpeg``.
    """
    # TODO(jhansche): Media class should expose this
    if media.get("__typename") == "MediaVideo":
        return "video/mp4"
    return "image/jpeg"


def _best_timedelta_title(other: datetime, now: datetime) -> str:
    """Format a media timestamp relative to now.

    Args:
        other: The media's creation timestamp.
        now: The current time to compare against.

    Returns:
        A human-readable, locally-formatted title for the timestamp.
    """
    # TODO(jhansche): find a better way to produce easily recognizable,
    # localized, and (as needed) relative datetimes.
    other = other.astimezone(dt_util.DEFAULT_TIME_ZONE).replace(microsecond=0)
    if other > now:
        # whoops?
        return other.strftime("%c")
    delta = now - other

    if delta.days < 1:
        # use "x <units> ago" relative string for < 24 hours
        # possibly "today <time>" or just "<H:m>" time
        return dt_util.get_age(other) + " ago"
    # if days == 1, "yesterday"? "yesterday %X"?

    if delta.days < 7:
        # 1-7 days, use "<dow> <time>"
        return other.strftime("%a, %X")

    if delta.days < 365:
        # within a year, full localized date+time
        return other.strftime("%c")

    # More than a year, show date only
    return other.strftime("%x")
