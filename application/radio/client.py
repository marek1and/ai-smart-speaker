import logging
from radios import RadioBrowser, Order
from config import RadioConfig

logger = logging.getLogger(__name__)


class RadioClient:
    def __init__(self, config: RadioConfig):
        self.config = config
        self.rb = RadioBrowser(user_agent="AI Smart Speaker")

    async def search_station(self, station_name: str) -> str | None:
        logger.info(
            "Searching for radio station '%s' in country '%s'",
            station_name,
            self.config.country,
        )
        try:
            stations = await self.rb.search(
                name=station_name,
                country=self.config.country,
                order=Order.CLICK_COUNT,
                reverse=True,
            )
        except Exception as e:
            logger.error("Error searching for station: %s", e)
            return None

        if not stations:
            logger.warning("No stations found for '%s'", station_name)
            return None

        # Find the best match (for now, the first result)
        # The radios library already sorts by popularity
        best_match = stations[0]
        logger.info("Found station: %s (%s)", best_match.name, best_match.url_resolved)
        return best_match.url_resolved
