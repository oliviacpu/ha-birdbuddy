"""Tests for the Bird Buddy recent-visitors tracker.

The event filter registered in RecentVisitors._start is worth exercising
through the real event bus: Home Assistant catches anything a filter raises
and skips the listener (see homeassistant/core.py, _async_dispatch), so a
filter that mishandles its argument drops postcards silently instead of
surfacing an error.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from birdbuddy.feed import FeedNode
from birdbuddy.feeder import Feeder
from birdbuddy.media import Collection
import pytest

from custom_components.birdbuddy.const import EVENT_NEW_POSTCARD
from custom_components.birdbuddy.visitors import RecentVisitors

FEEDER_ID = "feeder1"


def _visitors(hass, client=None):
    """Build a recent-visitors tracker for the test feeder.

    Args:
        hass: The Home Assistant instance.
        client: The Bird Buddy client mock, or a fresh MagicMock.

    Returns:
        A recent-visitors tracker bound to feeder ``feeder1``.
    """
    feeder = Feeder({"id": FEEDER_ID, "name": "Bird Buddy"})
    return RecentVisitors(feeder, client or MagicMock(), hass)


def _img(name):
    """Build a MediaImage whose thumbnail URL matches feeder1.

    Args:
        name: The file name embedded in the thumbnail URL.

    Returns:
        A media mapping for the feeder's image.
    """
    return {
        "__typename": "MediaImage",
        "thumbnailUrl": f"https://example.invalid/feeder1/{name}",
        "createdAt": "2026-07-09T12:00:00.000Z",
    }


def _node(node_id, created, species_name, media_name):
    """Build a feed node with one image sighting.

    Args:
        node_id: The node and species id.
        created: The ``createdAt`` timestamp (ISO 8601 UTC, e.g.
            ``2026-07-09T12:00:00.000Z``), or None to omit it.
        species_name: The recognized species name.
        media_name: The image file name.

    Returns:
        A populated feed node.
    """
    data = {
        "id": node_id,
        "medias": [_img(media_name)],
        "species": [{"id": node_id, "name": species_name}],
    }
    if created:
        data["createdAt"] = created
    return FeedNode(data)


@pytest.mark.parametrize(
    ("fired_feeder_id", "expected_calls"),
    [(FEEDER_ID, 1), ("a-different-feeder", 0)],
    ids=["this feeder", "another feeder"],
)
async def test_event_filter_admits_only_this_feeder(
    hass,
    fired_feeder_id,
    expected_calls,
):
    """Postcards reach the handler only when the feeder id matches."""
    visitors = _visitors(hass)
    with (
        # Both stubs isolate the filter: the seeding job would call the API,
        # and the handler would rebuild the library models.
        patch.object(RecentVisitors, "_update_latest_visitor", AsyncMock()),
        patch.object(RecentVisitors, "_on_new_postcard", AsyncMock()) as handler,
    ):
        visitors.register_callback(lambda _visitors: None)
        await hass.async_block_till_done()

        hass.bus.async_fire(
            EVENT_NEW_POSTCARD,
            {"postcard_id": "pc1", "feeder_id": fired_feeder_id},
        )
        await hass.async_block_till_done()

    assert handler.call_count == expected_calls


async def test_on_new_postcard_sets_latest(hass):
    """_on_new_postcard rebuilds media and species from the slim payload."""
    visitors = _visitors(hass)
    event = MagicMock()
    event.data = {
        "postcard_id": "pc1",
        "feeder_id": FEEDER_ID,
        "species": [{"id": "s1", "name": "American Robin"}],
        "media": {
            "__typename": "MediaImage",
            "contentUrl": "https://example.invalid/c.jpg",
            "thumbnailUrl": "https://example.invalid/t.jpg",
            "createdAt": "2026-07-09T12:00:00.000Z",
        },
    }
    await visitors._on_new_postcard(event)

    assert visitors.latest_species is not None
    assert visitors.latest_species.name == "American Robin"
    assert visitors.latest_media is not None
    assert visitors.latest_media.content_url == "https://example.invalid/c.jpg"


async def test_on_new_postcard_without_data_is_ignored(hass):
    """A postcard with neither media nor species leaves the state unset."""
    visitors = _visitors(hass)
    event = MagicMock()
    event.data = {
        "postcard_id": "pc1",
        "feeder_id": FEEDER_ID,
        "species": [],
        "media": None,
    }
    await visitors._on_new_postcard(event)

    assert visitors.latest_media is None
    assert visitors.latest_species is None


async def test_update_latest_visitor_prefers_newest_dated_item(hass):
    """Feed seeding picks the newest dated sighting, skipping undated ones."""
    feed = MagicMock()
    feed.filter = MagicMock(
        return_value=[
            _node("a", "2026-07-08T12:00:00.000Z", "American Robin", "o"),
            _node("b", "2026-07-09T12:00:00.000Z", "Blue Jay", "new.jpg"),
            _node("c", None, "Crow", "x"),
        ]
    )
    client = MagicMock()
    client.feed = AsyncMock(return_value=feed)
    visitors = _visitors(hass, client)

    await visitors._update_latest_visitor()

    assert visitors.latest_species is not None
    assert visitors.latest_species.name == "Blue Jay"
    assert visitors.latest_media is not None
    assert "new.jpg" in visitors.latest_media.thumbnail_url


async def test_update_latest_visitor_falls_back_to_collection(hass):
    """With no feed sighting, seeding uses the latest matching collection."""
    feed = MagicMock()
    feed.filter = MagicMock(return_value=[])
    client = MagicMock()
    client.feed = AsyncMock(return_value=feed)
    col = Collection(
        {
            "id": "col1",
            "species": {"id": "s1", "name": "American Robin"},
            "visitLastTime": "2026-07-09T12:00:00.000Z",
            "coverCollectionMedia": {"feederName": "Bird Buddy"},
        }
    )
    client.refresh_collections = AsyncMock(return_value={"col1": col})
    visitors = _visitors(hass, client)

    await visitors._update_latest_visitor()

    assert visitors.latest_species is not None
    assert visitors.latest_species.name == "American Robin"


def _postcard(species_name, created, name="c.jpg"):
    """Build a new-postcard event.

    Args:
        species_name: The recognized species name.
        created: The media's ``createdAt`` timestamp.
        name: The media's file name.

    Returns:
        A mock event.
    """
    event = MagicMock()
    event.data = {
        "species": [{"id": species_name, "name": species_name}],
        "media": {
            "__typename": "MediaImage",
            "id": name,
            "contentUrl": f"https://example.invalid/{name}",
            "thumbnailUrl": f"https://example.invalid/t-{name}",
            "createdAt": created,
        },
    }
    return event


def _collection_client(cover):
    """Build a client with an empty feed and one matching collection.

    Args:
        cover: The collection's cover media mapping.

    Returns:
        A mock client.
    """
    feed = MagicMock()
    feed.filter = MagicMock(return_value=[])
    client = MagicMock()
    client.feed = AsyncMock(return_value=feed)
    col = Collection(
        {
            "id": "col1",
            "species": {"id": "s1", "name": "House Finch"},
            "visitLastTime": "2026-07-09T12:00:00.000Z",
            "coverCollectionMedia": {"feederName": "Bird Buddy", "media": cover},
        }
    )
    client.refresh_collections = AsyncMock(return_value={"col1": col})
    return client


async def test_collection_fallback_uses_cover_photo(hass):
    """With no feed sighting, the photo comes from the collection's cover."""
    visitors = _visitors(hass, _collection_client(_img("cover.jpg")))

    await visitors._update_latest_visitor()

    assert visitors.latest_species is not None
    assert visitors.latest_species.name == "House Finch"
    assert visitors.latest_media is not None
    assert "cover.jpg" in visitors.latest_media.thumbnail_url


async def test_replayed_older_postcards_do_not_replace_newest(hass):
    """Postcards replay newest first, and the newest stays the latest."""
    visitors = _visitors(hass)
    listener = MagicMock()
    visitors._listeners.add(listener)

    await visitors._on_new_postcard(_postcard("Blue Jay", "2026-10-27T18:47:15.000Z"))
    await visitors._on_new_postcard(
        _postcard("House Finch", "2026-10-25T20:23:28.000Z")
    )

    assert visitors.latest_species is not None
    assert visitors.latest_species.name == "Blue Jay"
    listener.assert_called_once()


async def test_same_postcard_refreshes_its_url(hass):
    """A replay of the same photo may carry a fresher signed URL."""
    visitors = _visitors(hass)
    created = "2026-10-27T18:47:15.000Z"

    await visitors._on_new_postcard(_postcard("Blue Jay", created, "old.jpg"))
    await visitors._on_new_postcard(_postcard("Blue Jay", created, "fresh.jpg"))

    assert visitors.latest_media is not None
    assert visitors.latest_media.content_url == "https://example.invalid/fresh.jpg"


async def test_latest_visitor_survives_restart(hass):
    """A postcard that has left the feed is still the latest after a restart."""
    before = _visitors(hass)
    await before._on_new_postcard(_postcard("Blue Jay", "2026-10-27T18:47:15.000Z"))

    # The collection's newest visitor is older than the restored postcard.
    after = _visitors(hass, _collection_client(_img("cover.jpg")))
    listener = MagicMock()
    after._listeners.add(listener)
    await after._update_latest_visitor()

    assert after.latest_species is not None
    assert after.latest_species.name == "Blue Jay"
    assert after.latest_media is not None
    assert after.latest_media.content_url == "https://example.invalid/c.jpg"
    assert listener.call_count == 2


async def test_older_feed_item_does_not_replace_restored_visitor(hass):
    """Seeding from the feed keeps a newer restored visitor."""
    before = _visitors(hass)
    await before._on_new_postcard(_postcard("Blue Jay", "2026-10-27T18:47:15.000Z"))

    feed = MagicMock()
    feed.filter = MagicMock(
        return_value=[_node("a", "2026-07-08T12:00:00.000Z", "American Robin", "o")]
    )
    client = MagicMock()
    client.feed = AsyncMock(return_value=feed)
    after = _visitors(hass, client)
    await after._update_latest_visitor()

    assert after.latest_species is not None
    assert after.latest_species.name == "Blue Jay"


async def test_late_listener_hears_about_expired_media(hass):
    """A listener registered late still gets a visitor with an expired URL."""
    visitors = _visitors(hass)
    expired = _postcard("Blue Jay", "2026-10-27T18:47:15.000Z", "c.jpg?Expires=1")
    await visitors._on_new_postcard(expired)
    visitors._listeners.add(MagicMock())

    late = MagicMock()
    remove = visitors.register_callback(late)

    late.assert_called_once_with(visitors)
    remove()
