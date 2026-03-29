import logging
import time
import asyncio
import numpy as np
from mpd import MPDClient, MPDError
from config import MPDConfig
import threading

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
        if hasattr(self, 'config'):
            logger.debug("MPDClientWrapper instance already initialized.")
            return

        self.config = config
        self.client = MPDClient()
        self.is_connected = False
        self._fade_task: asyncio.Task | None = None
        self._volume_lock = threading.Lock()

        # Volume state
        self._is_ducked = False
        self._restore_volume = self.config.default_playback_volume

    def initialize_state(self):
        """
        Reads the initial state from MPD. If music is playing, sets the
        current volume as the restore volume, unless it's at a ducked level.
        """
        logger.info("Initializing MPD state...")
        if self.is_playing():
            initial_volume = self.get_volume()
            if initial_volume is not None:
                # Only adopt the volume if it's not at a ducked level
                if initial_volume > self.config.volume_duck_percentage:
                    self._restore_volume = initial_volume
                    logger.info(
                        "MPD is playing on startup. Restore volume set to: %d%%",
                        self._restore_volume,
                    )
                else:
                    logger.info(
                        "MPD is playing on startup, but volume (%d%%) is at or below duck level. "
                        "Using default restore volume: %d%%",
                        initial_volume,
                        self._restore_volume,
                    )
            else:
                logger.warning(
                    "MPD is playing, but could not read volume. Restore volume remains at default: %d%%",
                    self._restore_volume,
                )
        else:
            logger.info("MPD is not playing on startup.")

    def _connect(self):
        if self.is_connected:
            return True
        try:
            self.client.connect(self.config.host, self.config.port)
            self.is_connected = True
            logger.debug(
                "Connected to MPD server at %s:%s", self.config.host, self.config.port
            )
            return True
        except (ConnectionRefusedError, MPDError) as e:
            logger.error("Could not connect to MPD server: %s", e)
            self.is_connected = False
            return False

    def disconnect(self):
        if self.is_connected:
            try:
                self.client.close()
                self.client.disconnect()
                logger.info("Disconnected from MPD server")
            except (MPDError, IOError) as e:
                logger.error("Error disconnecting from MPD: %s", e)
            finally:
                self.is_connected = False
                self.client = MPDClient()

    async def play_station(self, url: str):
        """Clears the playlist, adds a new station, plays it, and fades the volume in."""
        if not self._connect():
            return
        try:
            self.client.clear()
            self.client.add(url)
            # Set volume to 0 before playing to avoid a sudden blast of sound
            self._set_internal_volume(0)
            self.client.play()
            logger.info("Started playing station: %s", url)
            self._is_ducked = False
            # Fade in to the current restore volume, which respects user settings.
            await self._fade_to(self._restore_volume, self.config.volume_fade_in_seconds)
        except (MPDError, IOError) as e:
            logger.error("Error playing station: %s", e)
            self.disconnect()

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

    def _set_internal_volume(self, linear_volume: int):
        """Internal method to set volume after mapping."""
        final_volume = self._map_volume_to_curve(linear_volume)
        log_msg = f"Set MPD volume to {final_volume} (mapped from {linear_volume}%)"

        with self._volume_lock:
            if not self._connect():
                logger.error("Failed to connect to MPD to set volume.")
                return
            try:
                self.client.setvol(final_volume)
                logger.debug(log_msg)
            except (MPDError, IOError) as e:
                logger.error("Failed to set volume: %s. Attempting reconnect.", e)
                self.disconnect()

    async def set_volume(self, volume: int):
        """Public method to set volume with a fade, which also updates the restore state."""
        logger.debug("set_volume(volume=%d) called.", volume)
        linear_volume = max(0, min(100, volume))
        self._restore_volume = linear_volume
        logger.info("User set new restore volume to %d%%", self._restore_volume)

        await self._fade_to(linear_volume, self.config.volume_fade_in_seconds / 2)
        self._is_ducked = False
        logger.debug("set_volume() finished.")

    async def duck(self):
        """Ducks the current volume, cancelling any in-progress fade."""
        logger.debug("duck() called.")
        if self._fade_task and not self._fade_task.done():
            self._fade_task.cancel()
            await asyncio.sleep(0.05)

        if self._is_ducked:
            logger.debug("Volume is already ducked. Ignoring duck command.")
            return

        current_volume = self.get_volume()
        if current_volume is None:
            logger.warning("Could not get current volume. Ducking from restore_volume.")
            current_volume = self._restore_volume
        
        duck_percentage = self.config.volume_duck_percentage

        if current_volume <= duck_percentage:
            logger.info("Volume is already at or below duck level. Not ducking.")
            logger.debug("duck() finished early.")
            return

        logger.info("Ducking volume from %d%% to %d%%", current_volume, duck_percentage)
        self._set_internal_volume(duck_percentage)
        self._is_ducked = True
        logger.debug("duck() finished.")

    async def unduck(self):
        """Restores the volume to the user-defined restore_volume."""
        logger.debug("unduck() called.")
        if not self._is_ducked:
            logger.debug("unduck() finished early: not ducked.")
            return

        # Always restore to the user's desired volume, not the pre-duck volume.
        logger.info("Restoring volume to %d%%", self._restore_volume)
        await self._fade_to(self._restore_volume, self.config.volume_fade_in_seconds)
        self._is_ducked = False
        logger.debug("unduck() finished.")

    async def _fade_to(self, target_volume: int, duration: float):
        """Smoothly transitions the volume to a target level."""
        logger.debug("fade_to(target=%d, duration=%.2f) called.", target_volume, duration)
        if self._fade_task and not self._fade_task.done():
            logger.debug("Cancelling previous fade task.")
            self._fade_task.cancel()

        loop = asyncio.get_running_loop()
        self._fade_task = loop.create_task(
            asyncio.to_thread(self.__fade_volume_thread, target_volume, duration)
        )
        try:
            await self._fade_task
        except asyncio.CancelledError:
            logger.info("Fade task was cancelled.")

    def __fade_volume_thread(self, target_volume: int, duration: float):
        """Internal thread worker for volume fading."""
        start_volume = self.get_volume()

        # Retry loop if volume is not available immediately after playback starts
        retry_count = 0
        while start_volume is None and retry_count < 5:
            logger.debug("Volume not yet available, retrying... (%d/5)", retry_count + 1)
            time.sleep(0.05)
            start_volume = self.get_volume()
            retry_count += 1

        effective_start_volume = start_volume
        if effective_start_volume is None:
            # If we still can't get the volume, infer a sensible starting point.
            if target_volume < self.config.volume_duck_percentage:
                effective_start_volume = 0
            else:
                effective_start_volume = self.config.volume_duck_percentage
            logger.debug(
                "start_volume was None, starting fade from inferred volume: %d",
                effective_start_volume,
            )

        logger.debug(
            "Fade thread started: from %s to %d over %.2fs",
            start_volume,  # Log original start_volume for debugging
            target_volume,
            duration,
        )

        try:
            # Reduce steps to 25 to prevent MPD disconnect errors on rapid setvol commands.
            # 50 steps was causing instability with some MPD servers.
            num_steps = 25
            if duration <= 0:
                delay = 0
                num_steps = 1
            else:
                delay = duration / num_steps

            volume_steps = np.linspace(
                effective_start_volume, target_volume, num_steps, dtype=int
            )

            for volume in volume_steps:
                if self._fade_task.cancelled():
                    logger.debug("Fade thread loop cancelled.")
                    break
                self._set_internal_volume(volume)
                time.sleep(delay)

            if not self._fade_task.cancelled():
                self._set_internal_volume(target_volume)
        except Exception as e:
            logger.error("Error during volume fade: %s", e)
            self._set_internal_volume(target_volume)
        finally:
            logger.debug("Fade thread finished.")

    def get_volume(self) -> int | None:
        if not self._connect():
            return None
        try:
            status = self.client.status()
            volume_str = status.get("volume")
            if volume_str is not None:
                mapped_volume = int(volume_str)
                return self._map_volume_from_curve(mapped_volume)
            return None
        except (MPDError, IOError) as e:
            logger.error("Error getting volume: %s", e)
            self.disconnect()
            return None

    def get_restore_volume(self) -> int:
        """Returns the volume level that will be restored after unducking."""
        return self._restore_volume

    def is_playing(self) -> bool:
        if not self._connect():
            return False
        try:
            status = self.client.status()
            is_playing = status.get("state") == "play"
            return is_playing
        except (MPDError, IOError) as e:
            logger.error("Error getting status: %s", e)
            self.disconnect()
            return False

    def stop(self) -> bool:
        if not self._connect():
            return False
        try:
            self.client.stop()
            logger.info("Stopped MPD playback.")
            return True
        except (MPDError, IOError) as e:
            logger.error("Error stopping playback: %s", e)
            self.disconnect()
            return False

    def get_current_song_info(self) -> dict | None:
        if not self._connect():
            return None
        try:
            return self.client.currentsong()
        except (MPDError, IOError) as e:
            logger.error("Error getting current song info: %s", e)
            self.disconnect()
            return None

    def get_playlist_info(self) -> list:
        if not self._connect():
            return []
        try:
            return self.client.playlistinfo()
        except (MPDError, IOError) as e:
            logger.error("Error getting playlist info: %s", e)
            self.disconnect()
            return []

    async def play(self):
        """Plays the current playlist and fades the volume in."""
        if not self._connect():
            return
        try:
            # Set volume to 0 before playing to avoid a sudden blast of sound
            self._set_internal_volume(0)
            self.client.play()
            logger.info("Started playing.")
            self._is_ducked = False
            # Fade in to the current restore volume, which respects user settings.
            await self._fade_to(self._restore_volume, self.config.volume_fade_in_seconds)
        except (MPDError, IOError) as e:
            logger.error("Error playing station: %s", e)
            self.disconnect()
