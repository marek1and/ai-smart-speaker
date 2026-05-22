import asyncio
import logging
from typing import Optional
from datetime import datetime
from functions.registry import register_function
from openhab.client import OpenHabClient
from config import AppConfig
from mpd_client.client import MPDClientWrapper
from radio.client import RadioClient

logger = logging.getLogger(__name__)

config = AppConfig.from_yaml()
openhab_client = OpenHabClient(config.openhab)
radio_client = RadioClient(config.radio)

# MPD client is injected by the orchestrator at startup so all tool functions
# share the same connection and see the same duck/unduck state.
_mpd_client: Optional[MPDClientWrapper] = None


def inject_mpd_client(client: MPDClientWrapper) -> None:
    """Called once by AudioOrchestrator.start() to share its MPD client."""
    global _mpd_client
    _mpd_client = client


async def close_radio_client() -> None:
    """Close the RadioBrowser HTTP session. Called from orchestrator cleanup."""
    await radio_client.close()


def _mpd() -> MPDClientWrapper:
    if _mpd_client is None:
        raise RuntimeError("MPD client not injected — inject_mpd_client() must be called first")
    return _mpd_client


@register_function(name="play_internet_radio")
async def play_internet_radio(station_name: Optional[str] = None) -> dict:
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

    url = await radio_client.search_station(station_name)
    if url:
        return {"url": url, "name": station_name}
    else:
        return {
            "status": "error",
            "details": f"Could not find a station named {station_name}.",
        }


@register_function(name="stop_radio")
async def stop_radio() -> dict:
    """Stops the radio playback immediately."""
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
    playlist = await client.get_playlist_info()
    volume = client.get_restore_volume()

    is_radio_on_playlist = False
    station_name_on_playlist = None
    if playlist:
        first_item = playlist[0]
        station_name = first_item.get("name") or first_item.get("title")
        url = first_item.get("file", "")

        is_radio_keyword = (station_name and "radio" in station_name.lower()) or (
            "radio" in url.lower()
        ) or ("live" in url.lower())

        is_popular_station = False
        if station_name:
            popular_stations = config.radio.popular_stations
            is_popular_station = any(
                popular.lower() in station_name.lower() for popular in popular_stations
            )

        if is_radio_keyword or is_popular_station:
            is_radio_on_playlist = True
            station_name_on_playlist = station_name or "Unknown Radio"

    current_song = await client.get_current_song_info()
    currently_playing_station = None
    if current_song:
        currently_playing_station = current_song.get("title") or current_song.get("name")

    return {
        "playback_state": status.get("state"),  # play, stop, pause
        "volume": volume,
        "is_radio_on_playlist": is_radio_on_playlist,
        "station_name_on_playlist": station_name_on_playlist,
        "current_track_title": currently_playing_station or "Unknown",
    }


@register_function(name="set_playback_volume")
async def set_playback_volume(volume_percentage: int) -> dict:
    """
    Signals the intent to change the playback volume.
    The action is deferred until after the AI's verbal response.
    """
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


@register_function(name="get_openhab_item_state")
def get_openhab_item_state(item_name: str) -> str | None:
    """Gets the state of an item in OpenHab."""
    return openhab_client.get_openhab_item_state(item_name)


def _resolve_tv_channel(name: str) -> Optional[int]:
    """Case-insensitive lookup of channel name in config.tv.channels."""
    key = name.lower().strip()
    for ch_name, ch_num in config.tv.channels.items():
        if ch_name.lower() == key:
            return ch_num
    return None


def _send_tv_channel(channel_name: str, channel_num: int) -> None:
    openhab_client.set_openhab_item_state(config.tv.channel_item, str(channel_num))
    logger.info("TV channel set to %s (%d)", channel_name, channel_num)


async def _switch_channel_after_boot(channel_name: str, channel_num: int) -> None:
    """Wait for TV to boot then send channel command (runs as background task)."""
    power_item = config.tv.power_item
    timeout = config.tv.boot_wait_timeout
    interval = config.tv.boot_poll_interval
    elapsed = 0.0
    logger.info("Waiting for TV to boot (max %.1fs)...", timeout)
    while elapsed < timeout:
        await asyncio.sleep(interval)
        elapsed += interval
        state = openhab_client.get_openhab_item_state(power_item)
        if str(state).upper() == "ON":
            logger.info("TV confirmed ON after %.1fs, waiting %.1fs before channel switch", elapsed, config.tv.post_boot_delay)
            await asyncio.sleep(config.tv.post_boot_delay)
            break
    else:
        logger.warning("TV did not confirm ON within %.1fs — sending channel anyway", timeout)
    _send_tv_channel(channel_name, channel_num)


@register_function(name="watch_tv")
async def watch_tv(channel_name: Optional[str] = None) -> dict:
    """Turn on the TV and/or switch to a channel by name."""
    power_item = config.tv.power_item

    current_state = openhab_client.get_openhab_item_state(power_item)
    already_on = str(current_state).upper() == "ON"

    if not already_on:
        openhab_client.set_openhab_item_state(power_item, "ON")
        logger.info("TV power ON sent (was off)")

    if not channel_name:
        return {"status": "success", "message": "TV turned on." if not already_on else "TV is already on."}

    channel_num = _resolve_tv_channel(channel_name)
    if channel_num is None:
        return {
            "status": "partial_success",
            "message": f"TV {'turned on' if not already_on else 'is on'} but channel '{channel_name}' not recognised.",
        }

    if already_on:
        _send_tv_channel(channel_name, channel_num)
    else:
        asyncio.create_task(_switch_channel_after_boot(channel_name, channel_num))

    return {"status": "success", "message": f"TV on, switching to {channel_name}."}


@register_function(name="set_openhab_item_state")
def set_openhab_item_state(
    item_name: Optional[str] = None,
    state: Optional[str] = None,
    item: Optional[str] = None,
    **kwargs,
) -> bool:
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
    if kwargs:
        logger.debug("set_openhab_item_state: ignoring unexpected kwargs=%s", kwargs)
    return openhab_client.set_openhab_item_state(resolved_name, state)
