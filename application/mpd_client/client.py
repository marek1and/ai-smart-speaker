import logging
import asyncio
import numpy as np
from typing import Optional, Dict, Any, List
from mpd import MPDClient, MPDError, ConnectionError as MPDConnectionError
from config import MPDConfig

logger = logging.getLogger(__name__)


class MPDClientWrapper:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            logger.info("Creating new MPDClientWrapper singleton instance.")
            cls._instance = super(MPDClientWrapper, cls).__new__(cls)
        return cls._instance

    def __init__(self, config: MPDConfig):
        # Singleton pattern: prevent re-initialization
        if hasattr(self, "config"):
            logger.debug("MPDClientWrapper instance already initialized.")
            return

        self.config = config
        self.client = MPDClient()
        self.client.timeout = config.connection_timeout
        self.is_connected = False
        self._fade_task: Optional[asyncio.Task] = None
        self._mpd_lock = asyncio.Lock()

        # Volume state
        self._is_ducked = False
        self._restore_volume = self.config.default_playback_volume

        # One-shot cache set by stop()/play_station()/play() so that the next
        # is_playing() call returns immediately without a network round-trip.
        # None means "unknown — ask MPD".
        self._is_playing: Optional[bool] = None

    async def initialize_state(self):
        """Initializes and logs the MPD client state."""
        logger.info("Initializing MPD client state...")
        async with self._mpd_lock:
            await self._connect()
            status = await self._get_status_unsafe()
            logger.info(f"MPD status: {status}")

    async def _connect(self) -> bool:
        """Connects to the MPD server. Must be called within a lock."""
        if self.is_connected:
            return True
        try:
            await asyncio.to_thread(self.client.connect, self.config.host, self.config.port)
            self.is_connected = True
            logger.debug(
                "Connected to MPD server at %s:%s", self.config.host, self.config.port
            )
            await self._ensure_outputs_enabled_unsafe()
            return True
        except (MPDConnectionError, MPDError, OSError) as e:
            logger.error("Could not connect to MPD server: %s", e)
            await self._disconnect_unsafe()
            return False

    async def _ensure_outputs_enabled_unsafe(self):
        """Checks and enables any disabled MPD outputs. Must be called within a lock."""
        try:
            outputs = await asyncio.to_thread(self.client.outputs)
            for output in outputs:
                if output.get("outputenabled") == "0":
                    logger.info(f"Enabling MPD output: {output.get('outputname')}")
                    await asyncio.to_thread(self.client.enableoutput, output.get("outputid"))
        except (MPDError, OSError) as e:
            logger.error(f"Failed to ensure outputs are enabled: {e}")
            await self._disconnect_unsafe()

    async def disconnect(self):
        """Public method to disconnect from the MPD server."""
        async with self._mpd_lock:
            await self._disconnect_unsafe()

    async def _disconnect_unsafe(self):
        """Internal method to disconnect. Must be called within a lock."""
        if self.is_connected:
            try:
                await asyncio.to_thread(self.client.close)
                await asyncio.to_thread(self.client.disconnect)
                logger.info("Disconnected from MPD server")
            except (MPDError, IOError) as e:
                logger.error("Error disconnecting from MPD: %s", e)
            finally:
                self.is_connected = False
                self.client = MPDClient()
                self.client.timeout = self.config.connection_timeout or 5

    async def _reconnect_unsafe(self) -> bool:
        """Disconnect and immediately reconnect. Must be called within a lock.

        Used to recover from BrokenPipeError / ConnectionResetError without
        waiting for the next caller to trigger a lazy reconnect.
        """
        logger.info("MPD reconnecting after connection reset...")
        await self._disconnect_unsafe()
        return await self._connect()

    async def play_station(self, url: str):
        """Clears the playlist, adds a new station, plays it, and fades the volume in."""
        async with self._mpd_lock:
            if not await self._connect():
                return
            try:
                await asyncio.to_thread(self.client.clear)
                await asyncio.to_thread(self.client.add, url)
                await self._set_internal_volume_unsafe(0)
                await asyncio.to_thread(self.client.play)
                logger.info("Started playing station: %s", url)
                self._is_playing = True
                self._is_ducked = False
            except (BrokenPipeError, ConnectionResetError) as e:
                logger.warning("MPD connection reset during play_station (%s), reconnecting...", type(e).__name__)
                if await self._reconnect_unsafe():
                    try:
                        await asyncio.to_thread(self.client.clear)
                        await asyncio.to_thread(self.client.add, url)
                        await self._set_internal_volume_unsafe(0)
                        await asyncio.to_thread(self.client.play)
                        logger.info("Started playing station after reconnect: %s", url)
                        self._is_playing = True
                        self._is_ducked = False
                    except (MPDError, IOError) as retry_e:
                        logger.error("Failed to play station after reconnect: %s", retry_e)
                        await self._disconnect_unsafe()
                        return
                else:
                    return
            except (MPDError, IOError) as e:
                logger.error("Error playing station: %s", e)
                await self._disconnect_unsafe()
                return  # Exit after error

        # Run fade-in outside of the main lock to allow other operations
        await self._fade_to(self._restore_volume, self.config.volume_fade_in_seconds)


    @staticmethod
    def _map_volume_to_curve(percentage: int) -> int:
        """Maps a linear percentage to a square root curve (y = sqrt(x))."""
        if percentage <= 0:
            return 0
        if percentage >= 100:
            return 100
        mapped_val = (percentage / 100.0) ** 0.5
        return int(round(mapped_val * 100))

    @staticmethod
    def _map_volume_from_curve(mapped_percentage: int) -> int:
        """Maps a square root curve percentage back to linear (x = y^2)."""
        if mapped_percentage <= 0:
            return 0
        if mapped_percentage >= 100:
            return 100
        linear_val = (mapped_percentage / 100.0) ** 2
        return int(round(linear_val * 100))

    async def _set_internal_volume_unsafe(self, linear_volume: int):
        """
        Sets the internal MPD volume. Must be called within a lock.
        Handles potential state errors.
        """
        final_volume = self._map_volume_to_curve(linear_volume)
        log_msg = f"Set MPD volume to {final_volume} (mapped from {linear_volume}%)"

        try:
            await asyncio.to_thread(self.client.setvol, final_volume)
            logger.debug(log_msg)
        except MPDError as e:
            if "All outputs are disabled" in str(e):
                logger.warning(f"MPDError setting volume: {e}. Retrying after ensuring outputs are enabled.")
                await self._ensure_outputs_enabled_unsafe()
                try:
                    await asyncio.to_thread(self.client.setvol, final_volume)
                    logger.debug(f"Retry successful: {log_msg}")
                except (MPDError, IOError) as retry_e:
                    logger.error(f"Failed to set volume on retry: {retry_e}")
                    await self._disconnect_unsafe()
            else:
                logger.error(f"Failed to set volume: {e}. Disconnecting.")
                await self._disconnect_unsafe()
        except (IOError, MPDConnectionError) as e:
            logger.error(f"Failed to set volume due to connection error: {e}. Disconnecting.")
            await self._disconnect_unsafe()

    async def set_volume(self, volume: int):
        """
        Public method to set volume. Updates restore volume and fades if playing.
        """
        logger.debug(f"set_volume(volume={volume}) called.")
        linear_volume = max(0, min(100, volume))
        self._restore_volume = linear_volume
        logger.info(f"User set new restore volume to {self._restore_volume}%%")

        if not await self.is_playing():
            logger.debug("Ignoring volume fade because nothing is playing.")
            return

        # Fade to the new volume. Fade duration is halved for responsiveness.
        await self._fade_to(linear_volume, self.config.volume_fade_in_seconds / 2)
        self._is_ducked = False
        logger.debug("set_volume() finished.")

    async def duck(self):
        """Ducks the current volume if music is playing."""
        logger.debug("duck() called.")
        if not await self.is_playing():
            logger.debug("Ignoring duck command as nothing is playing.")
            return

        # Cancel any ongoing fade
        if self._fade_task and not self._fade_task.done():
            self._fade_task.cancel()
            try:
                await self._fade_task
            except asyncio.CancelledError:
                pass  # Expected

        if self._is_ducked:
            logger.debug("Volume is already ducked. Ignoring duck command.")
            return

        current_volume = await self.get_volume()
        if current_volume is None:
            logger.warning("Could not get current volume to duck. Assuming restore volume.")
            current_volume = self._restore_volume

        duck_percentage = self.config.volume_duck_percentage

        if current_volume <= duck_percentage:
            logger.info("Volume is already at or below duck level. Not ducking.")
            self._is_ducked = True
            return

        logger.info(f"Ducking volume from {current_volume}%% to {duck_percentage}%%")
        # Set volume directly without a fade for a quick duck
        async with self._mpd_lock:
            if await self._connect():
                await self._set_internal_volume_unsafe(duck_percentage)
        self._is_ducked = True
        logger.debug("duck() finished.")


    async def unduck(self):
        """Restores the volume to the user-defined restore_volume if music is playing."""
        logger.debug("unduck() called.")
        if not self._is_ducked:
            logger.debug("unduck() finished early: not ducked.")
            return

        self._is_ducked = False  # Set state immediately

        if not await self.is_playing():
            logger.debug("Ignoring unduck command as nothing is playing.")
            return

        logger.info(f"Restoring volume to {self._restore_volume}%%")
        await self._fade_to(self._restore_volume, self.config.volume_fade_in_seconds)
        logger.debug("unduck() finished.")

    async def _fade_to(self, target_volume: int, duration: float):
        """Smoothly transitions the volume to a target level."""
        logger.debug(f"fade_to(target={target_volume}, duration={duration:.2f}) called.")
        if self._fade_task and not self._fade_task.done():
            logger.debug("Cancelling previous fade task.")
            self._fade_task.cancel()
            try:
                await self._fade_task
            except asyncio.CancelledError:
                pass # Expected

        self._fade_task = asyncio.create_task(
            self._fade_volume_async(target_volume, duration)
        )
        try:
            await self._fade_task
        except asyncio.CancelledError:
            logger.info("Fade task was cancelled.")
            self._fade_task.cancel()
            raise

    async def _fade_volume_async(self, target_volume: int, duration: float):
        """Asynchronous coroutine for volume fading."""
        start_volume = await self.get_volume()

        # Retry loop if volume is not available immediately after playback starts
        retry_count = 0
        while start_volume is None and retry_count < 5:
            logger.debug(f"Volume not yet available, retrying... ({retry_count + 1}/5)")
            await asyncio.sleep(0.05)
            start_volume = await self.get_volume()
            retry_count += 1
        
        if start_volume is None:
            logger.warning(f"Could not get start volume for fade. Aborting fade.")
            async with self._mpd_lock:
                if await self._connect():
                    await self._set_internal_volume_unsafe(target_volume)
            return

        logger.debug(f"Fade async started: from {start_volume} to {target_volume} over {duration:.2f}s")
        num_steps = 25
        if duration <= 0:
            delay = 0
            num_steps = 1
        else:
            delay = duration / num_steps

        volume_steps = np.linspace(start_volume, target_volume, num_steps, dtype=int)

        try:
            for volume in volume_steps:
                # The lock is acquired per-step to avoid holding it for the whole fade duration
                async with self._mpd_lock:
                    if not await self._connect():
                        logger.error("Connection lost during fade. Aborting.")
                        break
                    await self._set_internal_volume_unsafe(volume)
                await asyncio.sleep(delay)

            # Ensure final volume is set
            async with self._mpd_lock:
                if await self._connect():
                    await self._set_internal_volume_unsafe(target_volume)

        except asyncio.CancelledError:
            logger.debug("Fade async coro was cancelled.")
            # Do not set final volume on cancellation, leave it where it was
            raise # Re-raise CancelledError
        except Exception as e:
            logger.error(f"Error during async volume fade: {e}")
            # Try to set the final volume as a fallback
            async with self._mpd_lock:
                 if await self._connect():
                    await self._set_internal_volume_unsafe(target_volume)
        finally:
            logger.debug("Fade async finished.")


    async def get_volume(self) -> Optional[int]:
        """Gets the current playback volume as a linear percentage."""
        async with self._mpd_lock:
            status = await self._get_status_unsafe()
            if status is None:
                return None
            volume_str = status.get("volume")
            if volume_str is not None:
                return self._map_volume_from_curve(int(volume_str))
            return None

    def get_restore_volume(self) -> int:
        """Returns the volume level that will be restored after unducking."""
        return self._restore_volume

    async def is_playing(self) -> bool:
        """Checks if MPD is currently playing.

        Uses a persistent local cache updated by stop/play operations and by
        any successful status network query, so callers never block on a slow
        or dead MPD socket after a known state change.
        """
        if self._is_playing is not None:
            return self._is_playing
        status = await self.get_status()
        return status.get("state") == "play" if status else False

    async def stop(self):
        """Stops MPD playback."""
        async with self._mpd_lock:
            if not await self._connect():
                return
            try:
                await self._run_mpd_cmd(self.client.stop)
                logger.info("Stopped MPD playback.")
                self._is_playing = False
                # Reconnect to flush any unsolicited MPD notifications (e.g. state-change
                # events) that land in the socket buffer after stop and would corrupt the
                # protocol state for the next command.
                await self._reconnect_unsafe()
            except asyncio.TimeoutError:
                pass
            except (MPDError, IOError, MPDConnectionError) as e:
                logger.error("Error stopping playback: %s", e)
                await self._disconnect_unsafe()

    async def get_status(self) -> Optional[Dict[str, Any]]:
        """Gets the current MPD status."""
        async with self._mpd_lock:
            return await self._get_status_unsafe()
    
    async def _run_mpd_cmd(self, cmd, *args):
        """Run a blocking python-mpd2 call in a thread with a hard timeout."""
        timeout = (self.config.connection_timeout or 5) + 1
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(cmd, *args),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.error("MPD command timed out after %.0fs, marking disconnected.", timeout)
            self.is_connected = False
            self.client = MPDClient()
            self.client.timeout = self.config.connection_timeout or 5
            raise

    async def _get_status_unsafe(self) -> Optional[Dict[str, Any]]:
        """Gets status without acquiring lock. Must be called from a locked context."""
        if not await self._connect():
            return None
        try:
            result = await self._run_mpd_cmd(self.client.status)
            if result:
                self._is_playing = result.get("state") == "play"
            return result
        except asyncio.TimeoutError:
            return None
        except (BrokenPipeError, ConnectionResetError) as e:
            logger.warning("MPD connection reset during status check (%s), reconnecting...", type(e).__name__)
            if await self._reconnect_unsafe():
                try:
                    result = await self._run_mpd_cmd(self.client.status)
                    if result:
                        self._is_playing = result.get("state") == "play"
                    return result
                except Exception as retry_e:
                    logger.error("MPD status failed after reconnect: %s", retry_e)
                    await self._disconnect_unsafe()
            return None
        except (MPDError, IOError, MPDConnectionError) as e:
            logger.error(f"Failed to get MPD status: {e}")
            await self._disconnect_unsafe()
            return None

    async def get_current_song_info(self) -> Optional[Dict[str, Any]]:
        """Gets information about the currently playing song."""
        async with self._mpd_lock:
            if not await self._connect():
                return None
            try:
                return await asyncio.to_thread(self.client.currentsong)
            except (MPDError, IOError, MPDConnectionError) as e:
                logger.error("Error getting current song info: %s", e)
                await self._disconnect_unsafe()
                return None

    async def get_playlist_info(self) -> List[Dict[str, Any]]:
        """Gets information for all songs in the current playlist."""
        async with self._mpd_lock:
            if not await self._connect():
                return []
            try:
                return await asyncio.to_thread(self.client.playlistinfo)
            except (MPDError, IOError, MPDConnectionError) as e:
                logger.error("Error getting playlist info: %s", e)
                await self._disconnect_unsafe()
                return []

    async def play(self):
        """Plays the current playlist and fades the volume in."""
        logger.info("Executing play command.")
        async with self._mpd_lock:
            if not await self._connect():
                return
            try:
                await self._set_internal_volume_unsafe(0)
                await asyncio.to_thread(self.client.play)
                logger.info("Playback started.")
                self._is_playing = True
                self._is_ducked = False
            except (MPDError, IOError, MPDConnectionError) as e:
                logger.error("Error starting playback: %s", e)
                await self._disconnect_unsafe()
                return
        
        await self._fade_to(self._restore_volume, self.config.volume_fade_in_seconds)
