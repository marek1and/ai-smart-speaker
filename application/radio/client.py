import asyncio
import logging
import requests
from radios import RadioBrowser, Order
from config import RadioConfig
from mpd_client.state import RadioStateManager

logger = logging.getLogger(__name__)

_SEARCH_TIMEOUT = 8.0
# How many RadioBrowser search hits to probe before giving up on a name search.
_SEARCH_CANDIDATES = 3


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

    def _check_url(self, url: str) -> bool:
        """True if the stream URL answers without an HTTP error status.

        Fail-open by design: only an explicit 4xx/5xx rejects a URL. Icecast
        servers speak enough dialects (ICY status lines, refused HEAD, slow
        first byte) that treating every transport hiccup as "dead" would throw
        away working stations. Redirects are followed — that is exactly how
        a moved stream announces its new location, and how a stream that moved
        to nowhere (302 to a 404) gets caught.
        """
        try:
            response = requests.get(
                url,
                stream=True,
                timeout=self.config.url_check_timeout,
                allow_redirects=True,
                headers={"User-Agent": "AI Smart Speaker"},
            )
            status = response.status_code
            response.close()
            if status >= 400:
                logger.warning("Stream URL %s answered HTTP %d — rejecting", url, status)
                return False
            return True
        except Exception as e:
            logger.debug("Stream URL check for %s inconclusive (%s) — accepting", url, e)
            return True

    async def _url_is_alive(self, url: str) -> bool:
        return await asyncio.to_thread(self._check_url, url)

    def _resolve_pin(self, key: str) -> tuple[object | None, str]:
        """Finds the pinned station for a query key, by keyword or official name.

        Returns (pin, canonical_key) — the key is normalised to the config
        keyword (e.g. "radio eska" → "eska") so cache lookups stay consistent.
        """
        pin = self.config.stations.get(key)
        if pin is not None:
            return pin, key
        for keyword, candidate in self.config.stations.items():
            if candidate.name.lower() == key:
                return candidate, keyword
        return None, key

    async def search_station(self, station_name: str) -> tuple[str, str, str] | None:
        """Returns (url, official_name, state_key) or None.

        Priority:
          1. URL pinned in the config (wins over everything — escape hatch for
             wrong RadioBrowser entries)
          2. URL cache in RadioStateManager, unless older than url_cache_ttl_hours
          3. Pinned station → UUID resolution via RadioBrowser → verify → cache
          4. RadioBrowser name search → verify candidates → cache
        state_key is always the canonical config keyword when the station is
        pinned, otherwise the lowercased query.
        """
        key = station_name.lower().strip()
        pin, key = self._resolve_pin(key)

        # 1. Hard-wired URL from the config — no network lookup, no cache.
        if pin is not None and pin.url:
            name = pin.name or station_name
            if self._state:
                # Mirror it into the state so URL→name lookups keep working when
                # MPD is driven from outside (get_name_by_url / get_key_by_url).
                self._state.update_station(key, pin.url, name)
            logger.info("Using pinned URL for '%s': %s", name, pin.url)
            return pin.url, name, key

        # 2. URL cache hit — no network call needed
        ttl_s = self.config.url_cache_ttl_hours * 3600
        if self._state:
            cached_url = self._state.get_cached_url(key, max_age_s=ttl_s)
            if cached_url:
                entry = self._state.get_entry(key)
                name = entry.name if entry else station_name
                logger.info("Cache hit for '%s': %s", station_name, cached_url)
                return cached_url, name, key

        # 3. Pinned station — resolve its UUID via RadioBrowser
        if pin is not None and pin.uuid:
            logger.info("Resolving pinned '%s' uuid=%s", station_name, pin.uuid)
            try:
                station = await asyncio.wait_for(
                    self.rb.station(uuid=pin.uuid), timeout=_SEARCH_TIMEOUT
                )
                if station:
                    name = pin.name or station.name
                    url = station.url_resolved
                    if await self._url_is_alive(url):
                        if self._state:
                            self._state.update_station(key, url, name)
                        logger.info("Resolved pinned station: %s (%s)", name, url)
                        return url, name, key
                    logger.warning(
                        "Pinned station '%s' resolved to a dead URL (%s), falling back to search",
                        station_name, url,
                    )
                else:
                    logger.warning("UUID lookup returned nothing for '%s'", pin.uuid)
            except asyncio.TimeoutError:
                logger.warning("UUID lookup timed out for '%s', falling back to search", pin.uuid)
            except Exception as e:
                logger.warning("UUID lookup error for '%s': %s, falling back to search", pin.uuid, e)

        # 4. RadioBrowser name search → first candidate that actually answers
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

        for candidate in stations[:_SEARCH_CANDIDATES]:
            if not await self._url_is_alive(candidate.url_resolved):
                continue
            if self._state:
                self._state.update_station(key, candidate.url_resolved, candidate.name)
            logger.info("Found station: %s (%s)", candidate.name, candidate.url_resolved)
            return candidate.url_resolved, candidate.name, key

        logger.error(
            "All %d candidates for '%s' returned an HTTP error",
            min(len(stations), _SEARCH_CANDIDATES), station_name,
        )
        return None
