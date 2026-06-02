import asyncio
import logging
from typing import TYPE_CHECKING, Optional

import aiomqtt

from config import MQTTConfig
from mpd_client.client import MPDClientWrapper
from radio.client import RadioClient
import metrics

if TYPE_CHECKING:
    from audio.sounds import SoundPlayer

logger = logging.getLogger(__name__)


class MQTTBridge:
    def __init__(
        self,
        config: MQTTConfig,
        mpd_client: MPDClientWrapper,
        radio_client: RadioClient,
        sound_player: Optional["SoundPlayer"] = None,
    ):
        self._config = config
        self._mpd = mpd_client
        self._radio = radio_client
        self._sound_player = sound_player
        self._publish_queue: asyncio.Queue[dict] = asyncio.Queue()

    def on_state_change(self, event: dict) -> None:
        """Sync callback registered with MPDClientWrapper. Enqueues MQTT publish."""
        self._publish_queue.put_nowait(event)

    async def run(self) -> None:
        """Main loop — connects to broker, subscribes, and handles publish/command tasks."""
        radio = f"{self._config.topic_prefix}/radio"
        will = aiomqtt.Will(
            topic=f"{radio}/state",
            payload="OFF",
            retain=True,
            qos=1,
        )
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=self._config.broker,
                    port=self._config.port,
                    identifier=self._config.client_id,
                    username=self._config.username,
                    password=self._config.password,
                    will=will,
                ) as client:
                    await client.subscribe(f"{radio}/command/#")
                    logger.info(
                        "MQTT connected to %s:%d, subscribed to %s/command/#",
                        self._config.broker, self._config.port, radio,
                    )
                    await self._publish_initial_state(client, radio)
                    metrics.MQTT_CONNECTED.set(1)
                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(self._publish_loop(client, radio))
                        tg.create_task(self._command_loop(client, radio))
            except Exception as e:
                metrics.MQTT_CONNECTED.set(0)
                logger.warning(
                    "MQTT error: %s (%s) — reconnecting in %.0fs",
                    type(e).__name__, e, self._config.reconnect_interval,
                )
                await asyncio.sleep(self._config.reconnect_interval)

    async def _publish_initial_state(self, client: aiomqtt.Client, radio: str) -> None:
        """Publishes current radio state on (re)connect so the broker retain is fresh."""
        state = "ON" if await self._mpd.is_playing() else "OFF"
        station = self._mpd.get_current_station_name() or ""
        volume = self._mpd.get_restore_volume()
        await client.publish(f"{radio}/state", state, retain=True)
        await client.publish(f"{radio}/station", station, retain=True)
        await client.publish(f"{radio}/volume", str(volume), retain=True)
        logger.debug("MQTT initial state published: state=%s station=%s volume=%d", state, station, volume)

    async def _publish_loop(self, client: aiomqtt.Client, radio: str) -> None:
        while True:
            event = await self._publish_queue.get()
            if "state" in event:
                await client.publish(f"{radio}/state", event["state"], retain=True)
            if "station" in event:
                await client.publish(f"{radio}/station", event["station"] or "", retain=True)
            if "volume" in event:
                await client.publish(f"{radio}/volume", str(event["volume"]), retain=True)

    async def _command_loop(self, client: aiomqtt.Client, radio: str) -> None:
        async for message in client.messages:
            topic = str(message.topic)
            payload = message.payload.decode().strip()
            if topic == f"{radio}/command/power":
                await self._handle_power(payload)
            elif topic == f"{radio}/command/station":
                await self._handle_station(payload)
            elif topic == f"{radio}/command/volume":
                await self._handle_volume(payload)

    def _confirm(self) -> None:
        if self._sound_player:
            from audio.sounds import SoundEvent
            self._sound_player.play(SoundEvent.CONFIRM_ACTION)

    async def _handle_power(self, payload: str) -> None:
        if payload.upper() == "ON":
            metrics.MQTT_COMMANDS.labels(command='power_on').inc()
            metrics.RADIO_PLAYS.labels(
                source='mqtt_power',
                station=self._mpd.get_current_station_name() or '',
            ).inc()
            self._confirm()
            await self._mpd.play()
        elif payload.upper() == "OFF":
            metrics.MQTT_COMMANDS.labels(command='power_off').inc()
            metrics.RADIO_STOPS.labels(source='mqtt').inc()
            await self._mpd.stop()

    async def _handle_station(self, payload: str) -> None:
        if not payload:
            return
        url = await self._radio.search_station(payload)
        if not url:
            logger.warning("MQTT: station '%s' not found", payload)
            return
        metrics.MQTT_COMMANDS.labels(command='station').inc()
        metrics.RADIO_PLAYS.labels(source='mqtt_station', station=payload).inc()
        if await self._mpd.is_playing():
            await self._mpd.play_station(url, payload)
        else:
            await self._mpd.load_station(url, payload)

    async def _handle_volume(self, payload: str) -> None:
        try:
            metrics.MQTT_COMMANDS.labels(command='volume').inc()
            metrics.RADIO_VOLUME_CHANGES.labels(source='mqtt').inc()
            await self._mpd.set_volume(int(payload))
        except ValueError:
            logger.warning("MQTT: invalid volume payload '%s'", payload)
