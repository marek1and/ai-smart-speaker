import asyncio
import logging
from radios import RadioBrowser, Order
from config import RadioConfig
from mpd_client.state import RadioStateManager

logger = logging.getLogger(__name__)

_SEARCH_TIMEOUT = 8.0


class RadioClient:
    def __init__(self, config: RadioConfig) -> None:
        self.config = config
        self.rb = RadioBrowser(user_agent="AI Smart Speaker")
        self._state: RadioStateManager | None = None

    def set_state(self, state: RadioStateManager) -> None:
        self._state = state

    async def close(self) -> None:
        try:
            await self.rb.close()
        except Exception:
            pass

    async def search_station(self, station_name: str) -> tuple[str, str, str] | None:
        """Returns (url, official_name, state_key) or None.

        Priority:
          1. URL cache in RadioStateManager (no network call)
          2. Pinned station → UUID resolution via RadioBrowser → cache result
          3. RadioBrowser name search → cache result
        state_key is always query_key (station_name.lower()) so cache lookups
        are consistent with how callers phrase the query.
        """
        key = station_name.lower().strip()

        # 1. URL cache hit — no network call needed
        if self._state:
            cached_url = self._state.get_cached_url(key)
            if cached_url:
                entry = self._state.get_entry(key)
                name = entry.name if entry else station_name
                logger.info("Cache hit for '%s': %s", station_name, cached_url)
                return cached_url, name, key

        # 2. Pinned station — match by keyword or by official name
        pin = self.config.stations.get(key)
        if pin is None:
            for k, p in self.config.stations.items():
                if p.name.lower() == key:
                    pin = p
                    key = k  # normalise to canonical keyword
                    break
            # Re-check cache with normalised key (e.g. "radio eska" → "eska")
            if pin is not None and self._state:
                cached_url = self._state.get_cached_url(key)
                if cached_url:
                    entry = self._state.get_entry(key)
                    name = entry.name if entry else station_name
                    logger.info("Cache hit for '%s' (normalised to '%s'): %s", station_name, key, cached_url)
                    return cached_url, name, key
        if pin:
            logger.info("Resolving pinned '%s' uuid=%s", station_name, pin.uuid)
            try:
                station = await asyncio.wait_for(
                    self.rb.station(uuid=pin.uuid), timeout=_SEARCH_TIMEOUT
                )
                if station:
                    name = pin.name or station.name
                    url = station.url_resolved
                    if self._state:
                        self._state.update_station(key, url, name)
                    logger.info("Resolved pinned station: %s (%s)", name, url)
                    return url, name, key
                logger.warning("UUID lookup returned nothing for '%s'", pin.uuid)
            except asyncio.TimeoutError:
                logger.warning("UUID lookup timed out for '%s', falling back to search", pin.uuid)
            except Exception as e:
                logger.warning("UUID lookup error for '%s': %s, falling back to search", pin.uuid, e)

        # 3. RadioBrowser name search → cache result under query key
        logger.info("Searching RadioBrowser for '%s' in '%s'", station_name, self.config.country)
        try:
            stations = await asyncio.wait_for(
                self.rb.search(
                    name=station_name,
                    country=self.config.country,
                    order=Order.CLICK_COUNT,
                    reverse=True,
                ),
                timeout=_SEARCH_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.error("RadioBrowser search timed out for '%s'", station_name)
            return None
        except Exception as e:
            logger.error("RadioBrowser search error for '%s': %s", station_name, e)
            return None

        if not stations:
            logger.warning("No stations found for '%s'", station_name)
            return None

        best = stations[0]
        if self._state:
            self._state.update_station(key, best.url_resolved, best.name)
        logger.info("Found station: %s (%s)", best.name, best.url_resolved)
        return best.url_resolved, best.name, key
