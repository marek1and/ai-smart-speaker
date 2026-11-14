"""Playback the model asked for through a deferred tool: music, radio, volume."""

import asyncio
import logging
import time
from typing import Callable, Optional

from config import AppConfig
from functions.definitions import get_radio_client, wait_for_music
import metrics
from mpd_client.client import MPDClientWrapper
from state import DeferredAction

logger = logging.getLogger(__name__)


class DeferredPlayback:
    """Runs the deferred tool actions of a turn.

    Music starts as soon as its tool ran (on_action), everything else when the
    reply is over (dispatch). Requests that start playback are numbered: only
    the latest may start, whatever order their starts finish in.
    """

    def __init__(
        self,
        config: AppConfig,
        mpd_client: MPDClientWrapper,
        spawn: Callable[..., asyncio.Task],
        in_turn: Callable[[], bool],
        play_error_sound: Callable[[], None],
    ) -> None:
        self.config = config
        self.mpd_client = mpd_client
        self._spawn = spawn
        self._in_turn = in_turn
        self._play_error_sound = play_error_sound
        self._request = 0

    def on_action(self, action: DeferredAction) -> None:
        """Starts music as soon as the tool ran, not after the reply: Gemini
        often takes 10-20 s to finish speaking after a tool call (01.11.2026).
        The turn end only waits for this task."""
        payload = action.payload
        starts_playback = action.type == "play_music" or (
            action.type == "play_internet_radio"
            and payload.get("status") != "error"
            and (payload.get("url") or payload.get("action") == "play")
        )
        if starts_playback:
            # a radio call that changes nothing (already playing) must not cancel music
            self._request += 1
            action.request = self._request
        if action.type == "play_music":
            # "nie, puść Y" over the reply: start_new_turn drops the action, not
            # this task; the request number makes it give way to Y.
            action.task = self._spawn(
                self.start_music(payload, action.request), name="music_start"
            )

    async def start_music(self, payload: dict, request: Optional[int] = None) -> Optional[bool]:
        """Returns True/False for started/failed, None when superseded. Never
        raises: the turn end awaits it inside the state machine."""
        try:
            return await self._start_music_unsafe(payload, request)
        except Exception:
            logger.exception("Music start failed")
            self._play_error_sound()
            return False

    async def _start_music_unsafe(self, payload: dict, request: Optional[int]) -> Optional[bool]:
        # Taken when the tool ran: a "stop" (or another playback) meanwhile cancels the start.
        generation = payload.get("generation", self.mpd_client.playback_generation)
        # One deadline for the download and the play command together.
        deadline = time.monotonic() + self.config.music.start_timeout
        if payload.get("prefetched"):
            # Wait for the download outside the MPD lock: play would
            # otherwise hold it (and Mopidy's core) until it is done.
            if not await wait_for_music(payload["uris"][0], self.config.music.start_timeout):
                logger.warning("First track not downloaded within %.0fs, playing anyway",
                               self.config.music.start_timeout)
        if self.mpd_client.playback_generation != generation:
            logger.info("Music start superseded (stop or another playback)")
            return None
        if request is not None and request != self._request:
            logger.info("Music start superseded by a later playback request")
            return None
        # While the AI is still speaking the music starts at the duck level.
        in_turn = self._in_turn()
        # what is left of the deadline, but at least one ordinary MPD command
        play_timeout = max(deadline - time.monotonic(), float(self.config.mpd.connection_timeout))
        started = await self.mpd_client.play_music(
            payload["uris"],
            payload.get("title") or "",
            play_timeout,
            self.config.music.start_confirm_timeout,
            start_ducked=in_turn,
        )
        if not started:
            # The AI already said it is playing; without a sound the room just stays silent.
            self._play_error_sound()
        return started

    async def dispatch(self, actions: list[DeferredAction]) -> tuple[bool, Optional[str]]:
        """Execute the deferred actions of a turn.

        Returns (ran_radio_action, error): ran_radio_action is True if a radio
        play/stop action was executed (the turn ends without the end-conversation
        sound); error names an action that failed after the AI had confirmed it.
        """
        ran_radio_action = False
        error = None
        for action in actions:
            payload = action.payload

            if action.type == "play_internet_radio" and action.request not in (None, self._request):
                # music asked for later in this turn already started early
                logger.info("Deferred play_internet_radio superseded by a later music request")
                continue
            if action.type == "play_internet_radio":
                if payload.get("status") == "error":
                    logger.warning(
                        "play_internet_radio deferred error: %s", payload.get("details")
                    )
                elif payload.get("url"):
                    logger.info("Executing deferred action: play_internet_radio (url)")
                    station = payload.get("name") or ""
                    await self.mpd_client.play_station(
                        payload["url"], station, key=payload.get("key")
                    )
                    metrics.RADIO_PLAYS.labels(source="ai_new", station=station).inc()
                    ran_radio_action = True
                elif payload.get("action") == "play":
                    logger.info(
                        "Executing deferred action: play_internet_radio (resume)"
                    )
                    await self.mpd_client.resume(get_radio_client().search_station)
                    station = self.mpd_client.get_current_station_name() or "unknown"
                    metrics.RADIO_PLAYS.labels(
                        source="ai_resume", station=station
                    ).inc()
                    ran_radio_action = True

            elif action.type == "play_music":
                task = action.task
                logger.info("Deferred play_music: %s", "waiting for the early start" if task else "starting")
                started = await task if task else await self.start_music(payload)
                if started is False:
                    error = "music_start_failed"
                ran_radio_action = True

            elif action.type == "set_playback_volume":
                logger.info("Executing deferred action: set_playback_volume")
                if "volume_percentage" in payload:
                    await self.mpd_client.set_volume(payload["volume_percentage"])

        return ran_radio_action, error
