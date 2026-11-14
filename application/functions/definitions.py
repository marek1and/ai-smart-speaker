import asyncio
import re
import logging
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime
from functions.registry import register_function
from openhab.client import OpenHabClient
from homeassistant.client import HomeAssistantClient
from config import AppConfig, TVConfig, get_config
from mpd_client.client import MPDClientWrapper
from radio.client import RadioClient
from music.providers import KINDS, FallbackProvider, MopidyMusicProvider, create_provider
import metrics

logger = logging.getLogger(__name__)

# "Turn on" and "turn off" sound alike in Polish, so watch_tv() on an already-on TV
# almost always means the user asked to turn it off.
TV_ALREADY_ON_HINT = (
    "TV is already on. The command was most likely a misheard 'turn off' — "
    "turn the TV off now instead of asking the user to confirm."
)


@dataclass
class ToolContext:
    """What the tool functions work with.

    Built from the config when this module is imported: schemas.py reads the
    backend and the music provider then, to decide which tools the model gets.
    """

    config: AppConfig
    openhab: Optional[OpenHabClient]
    ha: Optional[HomeAssistantClient]
    radio: RadioClient
    music: MopidyMusicProvider | FallbackProvider | None
    # Set by AudioOrchestrator.start(), so all tool functions share its
    # connection and see the same duck/unduck state.
    mpd_client: Optional[MPDClientWrapper] = None
    # Queue entries added by prefetch; removed at the end of the turn in case
    # the deferred play never ran (a barge-in, a follow-up question).
    prefetched_tlids: list[int] = field(default_factory=list)
    # Strong references to fire-and-forget tasks (TV boot watchers). The event
    # loop only keeps weak refs — an unreferenced task can be garbage-collected
    # mid-flight and silently never finish.
    bg_tasks: set[asyncio.Task] = field(default_factory=set)

    @classmethod
    def from_config(cls, config: AppConfig) -> "ToolContext":
        return cls(
            config=config,
            openhab=(
                OpenHabClient(config.openhab)
                if config.openhab.url and config.openhab.api_key
                else None
            ),
            ha=(
                HomeAssistantClient(config.home_assistant)
                if config.home_assistant.url and config.home_assistant.api_key
                else None
            ),
            radio=RadioClient(config.radio),
            music=create_provider(config.music),
        )

    @property
    def smarthome_backend(self) -> str:
        """Which smart home tools the model gets: the clients above decide."""
        if self.ha is not None:
            return "home_assistant"
        return "openhab" if self.openhab is not None else "none"

    @property
    def mpd(self) -> MPDClientWrapper:
        if self.mpd_client is None:
            raise RuntimeError(
                "MPD client not injected — inject_mpd_client() must be called first"
            )
        return self.mpd_client

    def spawn(self, coro, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self.bg_tasks.add(task)
        task.add_done_callback(self.bg_tasks.discard)
        return task


ctx = ToolContext.from_config(get_config())


def inject_mpd_client(client: MPDClientWrapper) -> None:
    """Called once by AudioOrchestrator.start() to share its MPD client."""
    ctx.mpd_client = client
    ctx.radio.set_state(client.radio_state)


async def close_radio_client() -> None:
    """Close the RadioBrowser HTTP session. Called from orchestrator cleanup."""
    await ctx.radio.close()


def get_radio_client() -> RadioClient:
    return ctx.radio


@register_function(name="play_internet_radio")
async def play_internet_radio(station_name: Optional[str] = None) -> dict:
    """
    Finds a radio station URL or determines if playback should be resumed.
    The action is deferred and handled by the orchestrator after the AI response.
    """
    if not station_name:
        if ctx.mpd.source == "music" and ctx.mpd.get_current_station_name():
            # resume() switches from the music queue back to the last station.
            return {
                "action": "play",
                "for_model": {"already_playing": False, "volume": ctx.mpd.get_restore_volume()},
            }
        radio_status = await get_radio_status()
        if radio_status.get("is_radio_on_playlist"):
            # The play is deferred, so tell the model now whether it changes anything.
            # A frozen stream is not "playing": resume() reloads it.
            already_playing = (
                radio_status.get("playback_state") == "play"
                and not ctx.mpd.is_stream_stalled()
            )
            # Nothing to defer: a deferred play would restart a radio stopped meanwhile.
            return {
                **({} if already_playing else {"action": "play"}),
                "for_model": {
                    "already_playing": already_playing,
                    "volume": radio_status.get("volume"),
                },
            }
        else:
            return {
                "status": "error",
                "details": "No radio station is on the playlist. What station would you like to play?",
            }

    result = await ctx.radio.search_station(station_name)
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
    """Stops playback (radio or music) immediately."""
    metrics.RADIO_STOPS.labels(source="ai").inc()
    await ctx.mpd.stop()
    return {"status": "success", "message": "Playback stopped."}


@register_function(name="play_music")
async def play_music(query: str, kind: str = "track") -> dict:
    """Finds music for the query; the orchestrator starts it at once, ducked under the reply."""
    if ctx.music is None:
        return {"status": "error", "details": "Music playback is not configured."}
    if kind not in KINDS:
        kind = "track"
    # Before the search: a "stop" while it runs or while the AI speaks cancels the start.
    generation = ctx.mpd.playback_generation
    try:
        selection = await ctx.music.find(query, kind)
    except Exception:
        # The model must hear that it failed; the traceback tells a network
        # error from a bug in parsing Mopidy's reply.
        logger.exception("Music search for '%s' failed", query)
        return {"status": "error", "details": f"{ctx.music.label} search failed."}
    if selection is None:
        return {"status": "error", "details": f"Nothing found on {ctx.music.label} for '{query}'."}
    metrics.MUSIC_PLAYS.labels(provider=selection.source or ctx.music.label, kind=kind).inc()
    # Before prefetch, which creates the file.
    cached = ctx.music.is_cached(selection.uris[0])
    prefetched = False
    try:
        if not cached:
            tlid = await ctx.music.prefetch(selection.uris[0])
            if tlid is not None:
                ctx.prefetched_tlids.append(tlid)
                # Without cache_dir (cached is None) readiness cannot be seen:
                # the start does not wait, play blocks for the rest as before.
                prefetched = cached is False
    except Exception:
        # Deliberately not an error for the model: the track still resolves
        # on play, only later.
        logger.warning("Prefetch of %s failed", selection.uris[0], exc_info=True)
    for_model = {
        "title": selection.title,
        "artist": selection.artist,
        "tracks": len(selection.uris),
    }
    if selection.source:
        for_model["source"] = selection.source
    if cached is not None:
        for_model["starts"] = "now" if cached else "in_a_few_seconds"
    return {
        "uris": selection.uris,
        "title": selection.title,
        "prefetched": prefetched,
        "generation": generation,
        "for_model": for_model,
    }


async def wait_for_music(uri: str, timeout: float) -> bool:
    """Waits until the provider has the first track ready to play."""
    if ctx.music is None:
        return True
    return await ctx.music.wait_ready(uri, timeout)


async def drop_prefetched() -> None:
    """Removes prefetched entries left in Mopidy's queue (a no-op once
    play_music replaced the queue)."""
    if not ctx.prefetched_tlids or ctx.music is None:
        return
    tlids = list(ctx.prefetched_tlids)
    ctx.prefetched_tlids.clear()
    try:
        await ctx.music.drop(tlids)
    except Exception:
        # Deliberately only logged: a stray entry plays after the radio ends, nothing worse.
        logger.warning("Could not remove prefetched entries %s", tlids, exc_info=True)


@register_function(name="next_track")
async def next_track() -> dict:
    """Skips to the next track in the music queue, executed immediately."""
    client = ctx.mpd
    if client.source != "music":
        return {"status": "error", "details": "The radio is playing; it has no next track."}
    result, song, remaining = await client.next_track(start_ducked=True)
    if result == "no_next":
        return {"status": "no_next", "details": "That was the last track in the queue."}
    if result == "error":
        return {"status": "error", "details": "Could not switch tracks."}
    return {
        "status": "success",
        "title": song.get("title") or "Unknown",
        "artist": song.get("artist"),
        "remaining_tracks": remaining,
    }


@register_function(name="get_radio_status")
async def get_radio_status() -> dict:
    """
    Gets the current status of the radio, including playback state, volume,
    and whether a radio station is currently in the playlist.
    This function is executed immediately.
    """
    client = ctx.mpd
    status = await client.get_status() or {}
    volume = client.get_restore_volume()

    if client.source == "music":
        song = await client.get_current_song_info() or {}
        return {
            "playback_state": status.get("state"),
            "volume": volume,
            "source": "music",
            "is_radio_on_playlist": False,
            "current_track_title": song.get("title") or "Unknown",
            "current_track_artist": song.get("artist"),
            "remaining_tracks": client.remaining_tracks(status),
        }

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
        "source": "radio",
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
    metrics.RADIO_VOLUME_CHANGES.labels(source="ai").inc()
    # The previous level lets the model catch a direction mismatch,
    # e.g. "turn it up to 5%" at 52%.
    return {
        "volume_percentage": volume_percentage,
        "for_model": {"previous_volume": ctx.mpd.get_restore_volume()},
    }


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
    if ctx.openhab is None:
        logger.error("get_openhab_item_state: OpenHAB not configured")
        return _NO_SMARTHOME
    return await asyncio.to_thread(ctx.openhab.get_openhab_item_state, item_name)


def _resolve_tv_channel(name: str) -> Optional[int]:
    """Case-insensitive lookup of channel name in ctx.config.tv.channels."""
    key = name.lower().strip()
    for ch_name, ch_num in ctx.config.tv.channels.items():
        if ch_name.lower() == key:
            return ch_num
    return None


_HA_OFF_STATES = ("off", "unavailable", "unknown", "none")


class _HaTv:
    """The TV behind Home Assistant. tv.power_item may be a media_player
    entity, or any other domain HA can turn on/off (e.g. a switch.* wired to
    Wake-on-LAN for TVs whose media_player integration can't power the TV on
    itself); only a media_player takes channels."""

    backend = "HA"

    def __init__(self, client: HomeAssistantClient, tv_cfg: TVConfig) -> None:
        self._client = client
        self.entity_id = tv_cfg.power_item

    @property
    def can_switch_channels(self) -> bool:
        return self.entity_id.split(".")[0] == "media_player"

    def is_on(self) -> bool:
        state = self._client.get_entity_state(self.entity_id)
        return str(state).lower() not in _HA_OFF_STATES

    def turn_on(self) -> None:
        self._client.set_entity_state(self.entity_id, "ON")

    def set_channel(self, number: int) -> None:
        self._client.play_channel(self.entity_id, number)


class _OpenHabTv:
    """The TV behind OpenHAB: a power switch item and a channel number item."""

    backend = "OpenHAB"
    can_switch_channels = True

    def __init__(self, client: OpenHabClient, tv_cfg: TVConfig) -> None:
        self._client = client
        self._cfg = tv_cfg

    def is_on(self) -> bool:
        state = self._client.get_openhab_item_state(self._cfg.power_item)
        return str(state).upper() == "ON"

    def turn_on(self) -> None:
        self._client.set_openhab_item_state(self._cfg.power_item, "ON")

    def set_channel(self, number: int) -> None:
        self._client.set_openhab_item_state(self._cfg.channel_item, str(number))


def _tv() -> _HaTv | _OpenHabTv | None:
    if ctx.ha is not None and ctx.config.tv.power_item:
        return _HaTv(ctx.ha, ctx.config.tv)
    if ctx.openhab is not None:
        return _OpenHabTv(ctx.openhab, ctx.config.tv)
    return None


async def _send_tv_channel(tv: _HaTv | _OpenHabTv, channel_name: str, channel_num: int) -> None:
    await asyncio.to_thread(tv.set_channel, channel_num)
    logger.info("TV channel set to %s (%d) via %s", channel_name, channel_num, tv.backend)


async def _switch_channel_after_boot(
    tv: _HaTv | _OpenHabTv, channel_name: str, channel_num: int
) -> None:
    """Wait for the TV to boot, then send the channel (background task)."""
    tv_cfg = ctx.config.tv
    elapsed = 0.0
    logger.info("Waiting for TV to boot (max %.1fs)...", tv_cfg.boot_wait_timeout)
    while elapsed < tv_cfg.boot_wait_timeout:
        await asyncio.sleep(tv_cfg.boot_poll_interval)
        elapsed += tv_cfg.boot_poll_interval
        if await asyncio.to_thread(tv.is_on):
            logger.info(
                "TV confirmed ON after %.1fs, waiting %.1fs before channel switch",
                elapsed,
                tv_cfg.post_boot_delay,
            )
            await asyncio.sleep(tv_cfg.post_boot_delay)
            break
    else:
        logger.warning(
            "TV did not confirm ON within %.1fs — sending channel anyway",
            tv_cfg.boot_wait_timeout,
        )
    await _send_tv_channel(tv, channel_name, channel_num)


@register_function(name="watch_tv")
async def watch_tv(channel_name: Optional[str] = None) -> dict:
    """Turn on the TV and/or switch to a channel by name."""
    tv = _tv()
    if tv is None:
        return {"status": "error", "message": _NO_SMARTHOME}

    already_on = await asyncio.to_thread(tv.is_on)
    if not already_on:
        await asyncio.to_thread(tv.turn_on)
        metrics.TV_COMMANDS.labels(action="power_on").inc()
        logger.info("TV power ON sent via %s (was off)", tv.backend)

    if not channel_name:
        return {
            "status": "success",
            "message": TV_ALREADY_ON_HINT if already_on else "TV turned on.",
        }

    power = "is on" if already_on else "turned on"
    if not tv.can_switch_channels:
        logger.warning(
            "watch_tv: channel switching requires tv.power_item to be a media_player "
            "entity, got '%s'", ctx.config.tv.power_item,
        )
        return {
            "status": "partial_success",
            "message": f"TV {power} but channel switching isn't available for this entity.",
        }

    channel_num = _resolve_tv_channel(channel_name)
    if channel_num is None:
        return {
            "status": "partial_success",
            "message": f"TV {power} but channel '{channel_name}' not recognised.",
        }

    if already_on:
        await _send_tv_channel(tv, channel_name, channel_num)
    else:
        ctx.spawn(_switch_channel_after_boot(tv, channel_name, channel_num), name="tv_boot")

    metrics.TV_COMMANDS.labels(action="channel_switch").inc()
    return {"status": "success", "message": f"TV on, switching to {channel_name}."}


def _multi_set_result(names: list[str], results: list[bool], backend: str) -> dict:
    """Result of one state set on several items: which of them failed."""
    outcome = dict(zip(names, results))
    failed = [name for name, ok in outcome.items() if not ok]
    if not failed:
        return {"status": "success", "results": outcome}
    return {
        "status": "partial" if len(failed) < len(names) else "error",
        "failed": failed,
        "message": f"{backend} did not execute the command for: " + ", ".join(failed),
        "results": outcome,
    }


@register_function(name="set_openhab_item_state")
async def set_openhab_item_state(
    item_name: Optional[str] = None,
    state: Optional[str] = None,
    item: Optional[str] = None,
    **kwargs,
) -> dict:
    """Sets the state of an item in OpenHab.

    Accepts both ``item_name`` and ``item`` as the first argument so that
    hallucinated keyword names from OpenAI/Gemini do not cause a crash.
    Any extra unexpected keyword arguments are silently absorbed via **kwargs.
    Returns a status dict, like set_ha_entity_state: a bare False let the
    model confirm an action that did not happen.
    """
    resolved_name = item_name or item
    if ctx.openhab is None:
        logger.error("set_openhab_item_state: OpenHAB not configured")
        return {"status": "error", "message": _NO_SMARTHOME}
    if not resolved_name:
        logger.warning(
            "set_openhab_item_state called without item_name/item — ignoring. kwargs=%s",
            kwargs,
        )
        return {"status": "error", "message": "No item_name provided."}
    if kwargs:
        logger.debug("set_openhab_item_state: ignoring unexpected kwargs=%s", kwargs)
    ok = await asyncio.to_thread(ctx.openhab.set_openhab_item_state, resolved_name, state)
    if ok:
        return {"status": "success"}
    return {
        "status": "error",
        "message": f"OpenHAB did not execute the command for {resolved_name}.",
    }


@register_function(name="get_openhab_items_state")
async def get_openhab_items_state(item_names: Optional[list[str]] = None) -> dict:
    """
    Gets the states of multiple OpenHAB items in a single call.
    Use for aggregate questions ("are all lights off?", "are any windows open?")
    instead of calling get_openhab_item_state once per item.
    """
    if ctx.openhab is None:
        logger.error("get_openhab_items_state called but OpenHAB is not configured")
        return {"status": "error", "message": _NO_SMARTHOME}
    if not item_names:
        logger.warning("get_openhab_items_state called without item_names — ignoring.")
        return {"status": "error", "message": "No item_names provided."}
    states = await asyncio.to_thread(ctx.openhab.get_items_state, item_names)
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
    if ctx.openhab is None:
        logger.error("set_openhab_items_state called but OpenHAB is not configured")
        return {"status": "error", "message": _NO_SMARTHOME}
    if not item_names:
        logger.warning("set_openhab_items_state called without item_names — ignoring.")
        return {"status": "error", "message": "No item_names provided."}
    if kwargs:
        logger.debug("set_openhab_items_state: ignoring unexpected kwargs=%s", kwargs)
    results = await asyncio.gather(
        *(
            asyncio.to_thread(ctx.openhab.set_openhab_item_state, name, state)
            for name in item_names
        )
    )
    return _multi_set_result(item_names, results, "OpenHAB")


# ---------------------------------------------------------------------------
# Home Assistant functions
# ---------------------------------------------------------------------------


@register_function(name="get_ha_entity_state")
async def get_ha_entity_state(entity_id: str) -> str | None:
    """Gets the state of any Home Assistant entity."""
    if ctx.ha is None:
        logger.error("get_ha_entity_state called but Home Assistant is not configured")
        return None
    return await asyncio.to_thread(ctx.ha.get_entity_state, entity_id)


@register_function(name="get_ha_entities_state")
async def get_ha_entities_state(entity_ids: Optional[list[str]] = None) -> dict:
    """
    Gets the states of multiple Home Assistant entities in a single call.
    Use for aggregate questions ("are all lights off?", "are any windows open?")
    instead of calling get_ha_entity_state once per entity — this fetches all
    requested entities in one round trip.
    """
    if ctx.ha is None:
        logger.error("get_ha_entities_state called but Home Assistant is not configured")
        return {"status": "error", "message": _NO_SMARTHOME}
    if not entity_ids:
        logger.warning("get_ha_entities_state called without entity_ids — ignoring.")
        return {"status": "error", "message": "No entity_ids provided."}
    states = await asyncio.to_thread(ctx.ha.get_states, entity_ids)
    missing = [eid for eid in entity_ids if eid not in states]
    if missing:
        logger.warning("get_ha_entities_state: entities not found: %s", missing)
    return {"states": states}


async def _no_change_off(entity_id: str, current: str) -> dict:
    """no_change for OFF: lists the lights of the same HA area that are still on.

    The room comes from HA's area registry, not from the entity id. Only lights:
    turning off "the rest of the room" for a switch could cut a TV or a pump.
    """
    domain = entity_id.split(".", 1)[0]
    states = (
        await asyncio.to_thread(ctx.ha.get_area_states, entity_id)
        if domain == "light"
        else None
    )
    still_on = sorted(e for e, s in (states or {}).items() if e != entity_id and s == "on")
    if still_on:
        message = (
            f"{entity_id} is already {current}; nothing was sent. In the same room "
            f"these are on: {', '.join(still_on)}. The user most likely meant them — "
            "turn them off now. Never turn anything on."
        )
    elif states is not None:
        message = (
            f"{entity_id} is already {current}; nothing was sent and nothing else in "
            "this room is on. Tell the user it is already off. Never turn anything on."
        )
    else:
        # not looked up (not a light, no area, or HA did not answer): claim nothing
        message = (
            f"{entity_id} is already {current}; nothing was sent. Tell the user it is "
            "already off. Never turn anything on."
        )
    return {
        "status": "no_change",
        "current_state": current,
        "on_in_same_room": still_on,
        "message": message,
    }


@register_function(name="set_ha_entity_state")
async def set_ha_entity_state(
    entity_id: Optional[str] = None,
    state: Optional[str] = None,
    **kwargs,
) -> dict:
    """Sets the state of a Home Assistant entity via the appropriate service call.

    Returns a status dict, not a bare bool: the model used to get `{"result": "False"}`
    and still confirm the action ("Turning off the TV") — an explicit status with a
    message gives it an unambiguous signal that the operation failed.
    """
    if ctx.ha is None:
        logger.error("set_ha_entity_state called but Home Assistant is not configured")
        return {"status": "error", "message": _NO_SMARTHOME}
    if not entity_id:
        logger.warning(
            "set_ha_entity_state called without entity_id — ignoring. kwargs=%s", kwargs
        )
        return {"status": "error", "message": "No entity_id provided."}
    if kwargs:
        logger.debug("set_ha_entity_state: ignoring unexpected kwargs=%s", kwargs)
    requested = (state or "").upper()
    if (
        requested in ("ON", "OFF")
        and entity_id.split(".", 1)[0] in ctx.config.home_assistant.power_guard_domains
    ):
        current = await asyncio.to_thread(ctx.ha.get_entity_state, entity_id)
        if current is not None and current.upper() == requested:
            logger.info(
                "set_ha_entity_state: %s is already %s — not sent, reporting no_change",
                entity_id, current,
            )
            if requested == "OFF":
                return await _no_change_off(entity_id, current)
            return {
                "status": "no_change",
                "current_state": current,
                "message": (
                    f"{entity_id} is already {current}; nothing was sent. "
                    "The command was probably misheard."
                ),
            }
    ok = await asyncio.to_thread(ctx.ha.set_entity_state, entity_id, state or "")
    if ok:
        return {"status": "success"}
    return {
        "status": "error",
        "message": (
            f"Home Assistant did not execute the command for {entity_id} "
            "(no connectivity or unsupported state)."
        ),
    }


@register_function(name="set_ha_entities_state")
async def set_ha_entities_state(
    entity_ids: Optional[list[str]] = None,
    state: Optional[str] = None,
    **kwargs,
) -> dict:
    """Sets the same state on multiple Home Assistant entities in a single tool call (saves round trips)."""
    if ctx.ha is None:
        logger.error("set_ha_entities_state called but Home Assistant is not configured")
        return {"status": "error", "message": _NO_SMARTHOME}
    if not entity_ids:
        logger.warning("set_ha_entities_state called without entity_ids — ignoring.")
        return {"status": "error", "message": "No entity_ids provided."}
    if kwargs:
        logger.debug("set_ha_entities_state: ignoring unexpected kwargs=%s", kwargs)
    results = await asyncio.gather(
        *(
            asyncio.to_thread(ctx.ha.set_entity_state, eid, state or "")
            for eid in entity_ids
        )
    )
    return _multi_set_result(entity_ids, results, "Home Assistant")


_PROMPT_STATE_HEADER = (
    "Stan urządzeń odczytany z Home Assistant przy otwarciu tej rozmowy (mógł się "
    "od tego czasu zmienić, np. z włącznika). Na zwykłe pytania o te urządzenia "
    "odpowiadaj z tego odczytu; gdy użytkownik prosi, żeby sprawdzić, albo odczyt "
    "przeczy temu, co mówi, sprawdź narzędziem. Po własnych poleceniach przyjmij stan, "
    "który ustawiłaś. Wyraźne polecenie wykonuj zawsze, także gdy według odczytu nic "
    "nie trzeba zmieniać (reguła 5)."
)


def _prompt_state_entities() -> list[str]:
    """Entity ids of the configured domains, as the system prompt lists them."""
    domains = ctx.config.home_assistant.prompt_state_domains
    if not domains:
        return []
    pattern = r"\b(?:" + "|".join(re.escape(d) for d in domains) + r")\.[a-z0-9_]+"
    return sorted(set(re.findall(pattern, ctx.config.live.system_instruction or "")))


def _describe_state(item: dict) -> str:
    state = item.get("state", "unknown")
    brightness = (item.get("attributes") or {}).get("brightness")
    if item["entity_id"].startswith("light.") and state == "on" and brightness is not None:
        return f"on, jasność {round(brightness * 100 / 255)}%"
    return state


async def prompt_state_context() -> str:
    """The prompt block with current states, or "" (no HA, disabled, HA too slow)."""
    entity_ids = _prompt_state_entities()
    if ctx.ha is None or not entity_ids:
        return ""
    try:
        items = await asyncio.wait_for(
            asyncio.to_thread(ctx.ha.get_state_objects, entity_ids),
            ctx.config.home_assistant.prompt_state_timeout,
        )
    except asyncio.TimeoutError:
        logger.warning("HA states not read within %.1fs, session starts without them",
                       ctx.config.home_assistant.prompt_state_timeout)
        return ""
    if not items:
        return ""
    lines = [f"- {i['entity_id']}: {_describe_state(i)}" for i in sorted(items, key=lambda i: i["entity_id"])]
    return _PROMPT_STATE_HEADER + "\n" + "\n".join(lines)
