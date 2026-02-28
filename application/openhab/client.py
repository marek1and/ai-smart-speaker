import requests
from logging import getLogger

from config import OpenHabConfig

logger = getLogger(__name__)


class OpenHabClient:
    def __init__(self, config: OpenHabConfig):
        self.base_url = f"{config.url}/rest"
        self.headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "text/plain",
        }

    def get_openhab_item_state(self, item_name: str):
        """Gets the state of an item."""
        try:
            response = requests.get(
                f"{self.base_url}/items/{item_name}/state", headers=self.headers
            )
            response.raise_for_status()
            return response.text
        except requests.exceptions.RequestException as e:
            logger.error(f"Error getting item state for {item_name}: {e}")
            return None

    def set_openhab_item_state(self, item_name: str, state: str):
        """Sets the state of an item."""
        try:
            response = requests.post(
                f"{self.base_url}/items/{item_name}",
                data=state,
                headers=self.headers,
            )
            response.raise_for_status()
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Error setting item state for {item_name}: {e}")
            return False
