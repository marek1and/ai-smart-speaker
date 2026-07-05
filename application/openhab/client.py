import requests
from logging import getLogger

from config import OpenHabConfig
import metrics

logger = getLogger(__name__)


class OpenHabClient:
    def __init__(self, config: OpenHabConfig):
        self.base_url = f"{config.url}/rest"
        self.timeout = config.request_timeout
        self.headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "text/plain",
        }

    def get_openhab_item_state(self, item_name: str):
        """Gets the state of an item."""
        try:
            response = requests.get(
                f"{self.base_url}/items/{item_name}/state",
                headers=self.headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            metrics.OPENHAB_REQUESTS.labels(method='get', status='ok').inc()
            return response.text
        except requests.exceptions.RequestException as e:
            logger.error(f"Error getting item state for {item_name}: {e}")
            metrics.OPENHAB_REQUESTS.labels(method='get', status='error').inc()
            return None

    def get_items_state(self, item_names: list[str]) -> dict[str, str]:
        """Returns {item_name: state} for the given items via a single OpenHAB API call."""
        try:
            response = requests.get(
                f"{self.base_url}/items", headers=self.headers, timeout=self.timeout
            )
            response.raise_for_status()
            metrics.OPENHAB_REQUESTS.labels(method='get', status='ok').inc()
        except requests.exceptions.RequestException as e:
            logger.error(f"Error listing items: {e}")
            metrics.OPENHAB_REQUESTS.labels(method='get', status='error').inc()
            return {}
        wanted = set(item_names)
        return {
            item["name"]: item.get("state")
            for item in response.json()
            if item["name"] in wanted
        }

    def set_openhab_item_state(self, item_name: str, state: str):
        """Sets the state of an item."""
        try:
            response = requests.post(
                f"{self.base_url}/items/{item_name}",
                data=state,
                headers=self.headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            metrics.OPENHAB_REQUESTS.labels(method='set', status='ok').inc()
            metrics.OPENHAB_ITEM_SETS.labels(item=item_name).inc()
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Error setting item state for {item_name}: {e}")
            metrics.OPENHAB_REQUESTS.labels(method='set', status='error').inc()
            return False
