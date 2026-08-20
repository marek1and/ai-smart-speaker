import asyncio
import logging
from typing import Optional
from datetime import datetime
from functions.registry import register_function
from openhab.client import OpenHabClient
from homeassistant.client import HomeAssistantClient
from config import get_config
from mpd_client.client import MPDClientWrapper
from radio.client import RadioClient
import metrics

logger = logging.getLogger(__name__)

# Polish "włącz"/"wyłącz" differ by one unstressed syllable and the transcript gets
# them wrong regularly, so watch_tv() on an already-on TV almost always means the
# user asked to turn it OFF. The bare "TV is already on." left the model asking for
# confirmation instead (observed 2026-08-18) — the reply says what to do next.
TV_ALREADY_ON_HINT = (
    "TV is already on. The command was most likely a misheard 'turn off' — "
    "turn the TV off now instead of asking the user to confirm."
)

config = get_config()

# Strong references to fire-and-forget tasks (TV boot watchers). The event
# loop only keeps weak refs — an unreferenced task can be garbage-collected
# mid-flight and silently never finish.
_bg_tasks: set[asyncio.Task] = set()


def _spawn(coro, name: str) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task
openhab_client = (
    OpenHabClient(config.openhab)
    if config.openhab.url and config.openhab.api_key
    else None
)
ha_client = (
    HomeAssistantClient(config.home_assistant)
    if config.home_assistant.url and config.home_assistant.api_key
    else None
)
# Single source of truth for which smarthome backend is active — schemas.py
# reads this to decide which function declarations to expose to the AI, so
# it always matches the clients actually instantiated here.
ACTIVE_SMARTHOME_BACKEND = (
    "home_assistant" if ha_client is not None
    else "openhab" if openhab_client is not None
    else "none"
)
radio_client = RadioClient(config.radio)

# MPD client is injected by the orchestrator at startup so all tool functions
# share the same connection and see the same duck/unduck state.
_mpd_client: Optional[MPDClientWrapper] = None


def inject_mpd_client(client: MPDClientWrapper) -> None:
    """Called once by AudioOrchestrator.start() to share its MPD client."""
    global _mpd_client
    _mpd_client = client
    radio_client.set_state(client.radio_state)


async def close_radio_client() -> None:
    """Close the RadioBrowser HTTP session. Called from orchestrator cleanup."""
    await radio_client.close()


def get_radio_client() -> RadioClient:
    return radio_client


def _mpd() -> MPDClientWrapper:
    if _mpd_client is None:
        raise RuntimeError(
            "MPD client not injected — inject_mpd_client() must be called first"
        )
    return _mpd_client


@register_function(name="play_internet_radio")
async def play_internet_radio(station_name: Optional[str] = None) -> dict:
    metrics.AI_TOOL_CALLS.labels(function="play_internet_radio").inc()
    """
    Finds a radio station URL or determines if playback should be resumed.
    The action is deferred and handled by the orchestrator after the AI response.
    """
    if not station_name:
        radio_status = await get_radio_status()
        if radio_status.get("is_radio_on_playlist"):
            return {"action": "play"}
        else:
            return {
                "status": "error",
                "details": "No radio station is on the playlist. What station would you like to play?",
            }

    result = await radio_client.search_station(station_name)
    if result:
        url, official_name, key = result
        return {"url": url, "name": official_name, "key": key}
    else:
        return {
            "status": "error",
            "details": f"Could not find a station named {station_name}.",
        }


@register_function(name="stop_radio")
async def stop_radio() -> dict:
    """Stops the radio playback immediately."""
    metrics.AI_TOOL_CALLS.labels(function="stop_radio").inc()
    metrics.RADIO_STOPS.labels(source="ai").inc()
    await _mpd().stop()
    return {"status": "success", "message": "Radio playback stopped."}


@register_function(name="get_radio_status")
async def get_radio_status() -> dict:
    """
    Gets the current status of the radio, including playback state, volume,
    and whether a radio station is currently in the playlist.
    This function is executed immediately.
    """
    client = _mpd()
    status = await client.get_status() or {}
    volume = client.get_restore_volume()

    # 1. current_station_key → name from state
    station_name = client.get_current_station_name()

    current_song = None
    if not station_name:
        current_song = await client.get_current_song_info()
        if current_song:
            # 2. URL-based lookup against station list (handles external MPD clients)
            playlist_url = current_song.get("file") or ""
            station_name = client.radio_state.get_name_by_url(playlist_url)
            # 3. MPD metadata fallback
            if not station_name:
                station_name = current_song.get("title") or current_song.get("name")

    is_radio_on_playlist = station_name is not None

    return {
        "playback_state": status.get("state"),
        "volume": volume,
        "is_radio_on_playlist": is_radio_on_playlist,
        "station_name_on_playlist": station_name,
        "current_track_title": station_name or "Unknown",
    }


@register_function(name="set_playback_volume")
async def set_playback_volume(volume_percentage: int) -> dict:
    """
    Signals the intent to change the playback volume.
    The action is deferred until after the AI's verbal response.
    """
    metrics.AI_TOOL_CALLS.labels(function="set_playback_volume").inc()
    metrics.RADIO_VOLUME_CHANGES.labels(source="ai").inc()
    return {"volume_percentage": volume_percentage}


@register_function(name="request_for_user_input")
def request_for_user_input() -> dict:
    """
    Requests user input. This function is a dummy implementation
    as it is handled internally by the realtime managers.
    """
    return {"status": "success"}


@register_function(name="get_current_time")
def get_current_time() -> str:
    """Gets the current time."""
    return datetime.now().strftime("%H:%M:%S")


@register_function(name="get_current_date")
def get_current_date() -> str:
    """Gets the current date."""
    return datetime.now().strftime("%Y-%m-%d")


_NO_SMARTHOME = "Smart home backend not configured."


@register_function(name="get_openhab_item_state")
async def get_openhab_item_state(item_name: str) -> str | None:
    """Gets the state of an item in OpenHab."""
    if openhab_client is None:
        logger.error("get_openhab_item_state: OpenHAB not configured")
        return _NO_SMARTHOME
    return await asyncio.to_thread(openhab_client.get_openhab_item_state, item_name)


def _resolve_tv_channel(name: str) -> Optional[int]:
    """Case-insensitive lookup of channel name in config.tv.channels."""
    key = name.lower().strip()
    for ch_name, ch_num in config.tv.channels.items():
        if ch_name.lower() == key:
            return ch_num
    return None


# ---------------------------------------------------------------------------
# OpenHAB TV helpers
# ---------------------------------------------------------------------------


async def _oh_send_tv_channel(channel_name: str, channel_num: int) -> None:
    assert openhab_client is not None
    await asyncio.to_thread(
        openhab_client.set_openhab_item_state, config.tv.channel_item, str(channel_num)
    )
    logger.info("TV channel set to %s (%d) via OpenHAB", channel_name, channel_num)


async def _oh_switch_channel_after_boot(channel_name: str, channel_num: int) -> None:
    """Wait for TV to boot then send channel command via OpenHAB (background task)."""
    assert openhab_client is not None
    power_item = config.tv.power_item
    timeout = config.tv.boot_wait_timeout
    interval = config.tv.boot_poll_interval
    elapsed = 0.0
    logger.info("Waiting for TV to boot (max %.1fs)...", timeout)
    while elapsed < timeout:
        await asyncio.sleep(interval)
        elapsed += interval
        state = await asyncio.to_thread(openhab_client.get_openhab_item_state, power_item)
        if str(state).upper() == "ON":
            logger.info(
                "TV confirmed ON after %.1fs, waiting %.1fs before channel switch",
                elapsed,
                config.tv.post_boot_delay,
            )
            await asyncio.sleep(config.tv.post_boot_delay)
            break
    else:
        logger.warning(
            "TV did not confirm ON within %.1fs — sending channel anyway", timeout
        )
    await _oh_send_tv_channel(channel_name, channel_num)


# ---------------------------------------------------------------------------
# Home Assistant TV helpers
# ---------------------------------------------------------------------------


async def _ha_switch_channel_after_boot(
    channel_name: str, channel_num: int, entity_id: str
) -> None:
    """Wait for TV to boot then switch channel via HA media_player (background task)."""
    timeout = config.tv.boot_wait_timeout
    interval = config.tv.boot_poll_interval
    elapsed = 0.0
    logger.info("Waiting for TV to boot (max %.1fs)...", timeout)
    while elapsed < timeout:
        await asyncio.sleep(interval)
        elapsed += interval
        state = await asyncio.to_thread(ha_client.get_entity_state, entity_id)  # type: ignore[union-attr]
        if str(state).lower() not in ("off", "unavailable", "unknown", "none"):
            logger.info(
                "TV confirmed ON after %.1fs, waiting %.1fs before channel switch",
                elapsed,
                config.tv.post_boot_delay,
            )
            await asyncio.sleep(config.tv.post_boot_delay)
            break
    else:
        logger.warning(
            "TV did not confirm ON within %.1fs — sending channel anyway", timeout
        )
    await asyncio.to_thread(ha_client.play_channel, entity_id, channel_num)  # type: ignore[union-attr]
    logger.info("TV channel set to %s (%d) via HA", channel_name, channel_num)


@register_function(name="watch_tv")
async def watch_tv(channel_name: Optional[str] = None) -> dict:
    """Turn on the TV and/or switch to a channel by name."""
    metrics.AI_TOOL_CALLS.labels(function="watch_tv").inc()

    power_item = config.tv.power_item
    if ha_client is not None and power_item:
        # HA backend. power_item may be a media_player entity, or any other
        # domain HA can turn on/off (e.g. a switch.* wired to Wake-on-LAN for
        # TVs whose media_player integration can't power the TV on itself).
        power_domain = power_item.split(".")[0]
        current_state = await asyncio.to_thread(ha_client.get_entity_state, power_item)
        already_on = str(current_state).lower() not in (
            "off",
            "unavailable",
            "unknown",
            "none",
        )

        if not already_on:
            await asyncio.to_thread(ha_client.set_entity_state, power_item, "ON")
            metrics.TV_COMMANDS.labels(action="power_on").inc()
            logger.info("TV power ON sent via HA (was off)")

        if not channel_name:
            return {
                "status": "success",
                "message": "TV turned on."
            if not already_on
            else TV_ALREADY_ON_HINT,
            }

        if power_domain != "media_player":
            logger.warning(
                "watch_tv: channel switching requires power_item to be a media_player "
                "entity, got '%s' (domain=%s)", power_item, power_domain,
            )
            return {
                "status": "partial_success",
                "message": f"TV {'turned on' if not already_on else 'is on'} but channel switching isn't available for this entity.",
            }

        channel_num = _resolve_tv_channel(channel_name)
        if channel_num is None:
            return {
                "status": "partial_success",
                "message": f"TV {'turned on' if not already_on else 'is on'} but channel '{channel_name}' not recognised.",
            }

        if already_on:
            await asyncio.to_thread(ha_client.play_channel, power_item, channel_num)
            logger.info("TV channel set to %s (%d) via HA", channel_name, channel_num)
        else:
            _spawn(
                _ha_switch_channel_after_boot(channel_name, channel_num, power_item),
                name="ha_tv_boot",
            )

        metrics.TV_COMMANDS.labels(action="channel_switch").inc()
        return {"status": "success", "message": f"TV on, switching to {channel_name}."}

    # OpenHAB backend
    if openhab_client is None:
        return {"status": "error", "message": _NO_SMARTHOME}

    power_item = config.tv.power_item
    current_state = await asyncio.to_thread(openhab_client.get_openhab_item_state, power_item)
    already_on = str(current_state).upper() == "ON"

    if not already_on:
        await asyncio.to_thread(openhab_client.set_openhab_item_state, power_item, "ON")
        metrics.TV_COMMANDS.labels(action="power_on").inc()
        logger.info("TV power ON sent via OpenHAB (was off)")

    if not channel_name:
        return {
            "status": "success",
            "message": "TV turned on."
            if not already_on
            else TV_ALREADY_ON_HINT,
        }

    channel_num = _resolve_tv_channel(channel_name)
    if channel_num is None:
        return {
            "status": "partial_success",
            "message": f"TV {'turned on' if not already_on else 'is on'} but channel '{channel_name}' not recognised.",
        }

    if already_on:
        await _oh_send_tv_channel(channel_name, channel_num)
    else:
        _spawn(
            _oh_switch_channel_after_boot(channel_name, channel_num),
            name="oh_tv_boot",
        )

    metrics.TV_COMMANDS.labels(action="channel_switch").inc()
    return {"status": "success", "message": f"TV on, switching to {channel_name}."}


@register_function(name="set_openhab_item_state")
async def set_openhab_item_state(
    item_name: Optional[str] = None,
    state: Optional[str] = None,
    item: Optional[str] = None,
    **kwargs,
) -> bool:
    metrics.AI_TOOL_CALLS.labels(function="set_openhab_item_state").inc()
    """Sets the state of an item in OpenHab.

    Accepts both ``item_name`` and ``item`` as the first argument so that
    hallucinated keyword names from OpenAI/Gemini do not cause a crash.
    Any extra unexpected keyword arguments are silently absorbed via **kwargs.
    """
    resolved_name = item_name or item
    if not resolved_name:
        logger.warning(
            "set_openhab_item_state called without item_name/item — ignoring. kwargs=%s",
            kwargs,
        )
        return False
    if openhab_client is None:
        logger.error("set_openhab_item_state: OpenHAB not configured")
        return False
    if kwargs:
        logger.debug("set_openhab_item_state: ignoring unexpected kwargs=%s", kwargs)
    return await asyncio.to_thread(openhab_client.set_openhab_item_state, resolved_name, state)


@register_function(name="get_openhab_items_state")
async def get_openhab_items_state(item_names: Optional[list[str]] = None) -> dict:
    """
    Gets the states of multiple OpenHAB items in a single call.
    Use for aggregate questions ("are all lights off?", "are any windows open?")
    instead of calling get_openhab_item_state once per item.
    """
    metrics.AI_TOOL_CALLS.labels(function="get_openhab_items_state").inc()
    if openhab_client is None:
        logger.error("get_openhab_items_state called but OpenHAB is not configured")
        return {"status": "error", "message": _NO_SMARTHOME}
    if not item_names:
        logger.warning("get_openhab_items_state called without item_names — ignoring.")
        return {"status": "error", "message": "No item_names provided."}
    states = await asyncio.to_thread(openhab_client.get_items_state, item_names)
    missing = [name for name in item_names if name not in states]
    if missing:
        logger.warning("get_openhab_items_state: items not found: %s", missing)
    return {"states": states}


@register_function(name="set_openhab_items_state")
async def set_openhab_items_state(
    item_names: Optional[list[str]] = None,
    state: Optional[str] = None,
    **kwargs,
) -> dict:
    """Sets the same state on multiple OpenHAB items in a single tool call (saves round trips)."""
    metrics.AI_TOOL_CALLS.labels(function="set_openhab_items_state").inc()
    if openhab_client is None:
        logger.error("set_openhab_items_state called but OpenHAB is not configured")
        return {"status": "error", "message": _NO_SMARTHOME}
    if not item_names:
        logger.warning("set_openhab_items_state called without item_names — ignoring.")
        return {"status": "error", "message": "No item_names provided."}
    if kwargs:
        logger.debug("set_openhab_items_state: ignoring unexpected kwargs=%s", kwargs)
    results = await asyncio.gather(
        *(
            asyncio.to_thread(openhab_client.set_openhab_item_state, name, state)
            for name in item_names
        )
    )
    return {"results": dict(zip(item_names, results))}


# ---------------------------------------------------------------------------
# Home Assistant functions
# ---------------------------------------------------------------------------


@register_function(name="get_ha_entity_state")
async def get_ha_entity_state(entity_id: str) -> str | None:
    """Gets the state of any Home Assistant entity."""
    metrics.AI_TOOL_CALLS.labels(function="get_ha_entity_state").inc()
    if ha_client is None:
        logger.error("get_ha_entity_state called but Home Assistant is not configured")
        return None
    return await asyncio.to_thread(ha_client.get_entity_state, entity_id)


@register_function(name="get_ha_entities_state")
async def get_ha_entities_state(entity_ids: Optional[list[str]] = None) -> dict:
    """
    Gets the states of multiple Home Assistant entities in a single call.
    Use for aggregate questions ("are all lights off?", "are any windows open?")
    instead of calling get_ha_entity_state once per entity — this fetches all
    requested entities in one round trip.
    """
    metrics.AI_TOOL_CALLS.labels(function="get_ha_entities_state").inc()
    if ha_client is None:
        logger.error("get_ha_entities_state called but Home Assistant is not configured")
        return {"status": "error", "message": _NO_SMARTHOME}
    if not entity_ids:
        logger.warning("get_ha_entities_state called without entity_ids — ignoring.")
        return {"status": "error", "message": "No entity_ids provided."}
    states = await asyncio.to_thread(ha_client.get_states, entity_ids)
    missing = [eid for eid in entity_ids if eid not in states]
    if missing:
        logger.warning("get_ha_entities_state: entities not found: %s", missing)
    return {"states": states}


@register_function(name="set_ha_entity_state")
async def set_ha_entity_state(
    entity_id: Optional[str] = None,
    state: Optional[str] = None,
    **kwargs,
) -> bool:
    """Sets the state of a Home Assistant entity via the appropriate service call."""
    metrics.AI_TOOL_CALLS.labels(function="set_ha_entity_state").inc()
    if ha_client is None:
        logger.error("set_ha_entity_state called but Home Assistant is not configured")
        return False
    if not entity_id:
        logger.warning(
            "set_ha_entity_state called without entity_id — ignoring. kwargs=%s", kwargs
        )
        return False
    if kwargs:
        logger.debug("set_ha_entity_state: ignoring unexpected kwargs=%s", kwargs)
    return await asyncio.to_thread(ha_client.set_entity_state, entity_id, state or "")


@register_function(name="set_ha_entities_state")
async def set_ha_entities_state(
    entity_ids: Optional[list[str]] = None,
    state: Optional[str] = None,
    **kwargs,
) -> dict:
    """Sets the same state on multiple Home Assistant entities in a single tool call (saves round trips)."""
    metrics.AI_TOOL_CALLS.labels(function="set_ha_entities_state").inc()
    if ha_client is None:
        logger.error("set_ha_entities_state called but Home Assistant is not configured")
        return {"status": "error", "message": _NO_SMARTHOME}
    if not entity_ids:
        logger.warning("set_ha_entities_state called without entity_ids — ignoring.")
        return {"status": "error", "message": "No entity_ids provided."}
    if kwargs:
        logger.debug("set_ha_entities_state: ignoring unexpected kwargs=%s", kwargs)
    results = await asyncio.gather(
        *(
            asyncio.to_thread(ha_client.set_entity_state, eid, state or "")
            for eid in entity_ids
        )
    )
    return {"results": dict(zip(entity_ids, results))}
