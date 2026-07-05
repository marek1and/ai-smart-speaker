import requests
from logging import getLogger

from config import HomeAssistantConfig
import metrics

logger = getLogger(__name__)


class HomeAssistantClient:
    def __init__(self, config: HomeAssistantConfig):
        self.base_url = f"{config.url}/api"
        self.timeout = config.request_timeout
        self.headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        }

    def get_entity_state(self, entity_id: str) -> str | None:
        """Returns the state string of any HA entity."""
        try:
            response = requests.get(
                f"{self.base_url}/states/{entity_id}",
                headers=self.headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            metrics.HA_REQUESTS.labels(method='get', status='ok').inc()
            return response.json().get("state")
        except requests.exceptions.RequestException as e:
            logger.error("Error getting entity state for %s: %s", entity_id, e)
            metrics.HA_REQUESTS.labels(method='get', status='error').inc()
            return None

    def get_states(self, entity_ids: list[str]) -> dict[str, str]:
        """Returns {entity_id: state} for the given entities via a single HA API call."""
        try:
            response = requests.get(
                f"{self.base_url}/states", headers=self.headers, timeout=self.timeout
            )
            response.raise_for_status()
            metrics.HA_REQUESTS.labels(method='get', status='ok').inc()
        except requests.exceptions.RequestException as e:
            logger.error("Error listing entity states: %s", e)
            metrics.HA_REQUESTS.labels(method='get', status='error').inc()
            return {}
        wanted = set(entity_ids)
        return {
            s["entity_id"]: s["state"]
            for s in response.json()
            if s["entity_id"] in wanted
        }

    def call_service(self, domain: str, service: str, entity_id: str, **kwargs) -> bool:
        """Calls a HA service."""
        payload = {"entity_id": entity_id, **kwargs}
        try:
            response = requests.post(
                f"{self.base_url}/services/{domain}/{service}",
                json=payload,
                headers=self.headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            metrics.HA_REQUESTS.labels(method='set', status='ok').inc()
            return True
        except requests.exceptions.RequestException as e:
            logger.error("Error calling %s.%s for %s: %s", domain, service, entity_id, e)
            metrics.HA_REQUESTS.labels(method='set', status='error').inc()
            return False

    def set_entity_state(self, entity_id: str, state: str) -> bool:
        """
        Smart dispatcher: translates a state string to the appropriate HA service call.

        Supported formats by domain:
          light        — "ON"/"OFF", "0"-"100" (brightness%), "H,S,B" (hue 0-360, sat 0-100, bri 0-100)
          switch / input_boolean / fan — "ON"/"OFF"
          cover        — "0"-"100" (position: 0=closed, 100=open)
          media_player — "ON"/"OFF", "0"-"100" (volume%), "MUTE"/"UNMUTE"
        """
        domain = entity_id.split(".")[0]
        state_upper = state.strip().upper()

        if domain == "light":
            return self._set_light(entity_id, state, state_upper)
        elif domain in ("switch", "input_boolean", "fan"):
            svc = "turn_on" if state_upper in ("ON", "TRUE", "1") else "turn_off"
            metrics.HA_ENTITY_SETS.labels(entity=entity_id).inc()
            return self.call_service(domain, svc, entity_id)
        elif domain == "cover":
            try:
                pos = int(state.strip())
                metrics.HA_ENTITY_SETS.labels(entity=entity_id).inc()
                return self.call_service("cover", "set_cover_position", entity_id, position=pos)
            except ValueError:
                pass
        elif domain == "media_player":
            return self._set_media_player(entity_id, state, state_upper)

        logger.warning("Cannot map state '%s' for entity '%s' (domain: %s)", state, entity_id, domain)
        return False

    def _set_light(self, entity_id: str, state: str, state_upper: str) -> bool:
        if state_upper in ("ON", "TRUE"):
            metrics.HA_ENTITY_SETS.labels(entity=entity_id).inc()
            return self.call_service("light", "turn_on", entity_id)
        if state_upper in ("OFF", "FALSE", "0"):
            metrics.HA_ENTITY_SETS.labels(entity=entity_id).inc()
            return self.call_service("light", "turn_off", entity_id)

        # HSB format: "H,S,B"
        parts = state.strip().split(",")
        if len(parts) == 3:
            try:
                h, s, b = float(parts[0]), float(parts[1]), float(parts[2])
                metrics.HA_ENTITY_SETS.labels(entity=entity_id).inc()
                return self.call_service(
                    "light", "turn_on", entity_id,
                    hs_color=[h, s], brightness_pct=int(b),
                )
            except ValueError:
                pass

        # Brightness 1-100
        try:
            bri = int(state.strip())
            if bri <= 0:
                metrics.HA_ENTITY_SETS.labels(entity=entity_id).inc()
                return self.call_service("light", "turn_off", entity_id)
            metrics.HA_ENTITY_SETS.labels(entity=entity_id).inc()
            return self.call_service("light", "turn_on", entity_id, brightness_pct=bri)
        except ValueError:
            pass

        logger.warning("Cannot parse light state '%s' for %s", state, entity_id)
        return False

    def _set_media_player(self, entity_id: str, state: str, state_upper: str) -> bool:
        if state_upper == "ON":
            metrics.HA_ENTITY_SETS.labels(entity=entity_id).inc()
            return self.call_service("media_player", "turn_on", entity_id)
        if state_upper == "OFF":
            metrics.HA_ENTITY_SETS.labels(entity=entity_id).inc()
            return self.call_service("media_player", "turn_off", entity_id)
        if state_upper == "MUTE":
            metrics.HA_ENTITY_SETS.labels(entity=entity_id).inc()
            return self.call_service("media_player", "volume_mute", entity_id, is_volume_muted=True)
        if state_upper == "UNMUTE":
            metrics.HA_ENTITY_SETS.labels(entity=entity_id).inc()
            return self.call_service("media_player", "volume_mute", entity_id, is_volume_muted=False)

        try:
            vol = int(state.strip())
            metrics.HA_ENTITY_SETS.labels(entity=entity_id).inc()
            return self.call_service("media_player", "volume_set", entity_id, volume_level=round(vol / 100, 2))
        except ValueError:
            pass

        logger.warning("Cannot parse media_player state '%s' for %s", state, entity_id)
        return False

    def play_channel(self, entity_id: str, channel_num: int) -> bool:
        """Switches TV to a channel number via media_player.play_media."""
        return self.call_service(
            "media_player", "play_media", entity_id,
            media_content_type="channel",
            media_content_id=str(channel_num),
        )
