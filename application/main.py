#!/usr/bin/env python3
"""
AI Smart Speaker with Realtime API (Gemini / OpenAI).

Entry point for the voice assistant application.
"""

import asyncio
import logging
import sys

from orchestrator import AudioOrchestrator
import functions.definitions
import metrics
from config import get_config

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

# python-mpd2 logs every connect/disconnect at INFO ("Calling MPD connect(...)").
# We reconnect after each stop() to flush the command socket, so that chatter shows up
# as three lines per radio stop with nothing to say.
logging.getLogger("mpd").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


async def run() -> None:
    """Main entry point."""

    # Load configuration from YAML (with defaults)
    config = get_config()

    logger.info("Realtime provider: %s", config.live.provider)

    if config.metrics.enabled:
        metrics.start(config.metrics.port, provider=config.live.provider)
        logger.info("Prometheus metrics on port %d", config.metrics.port)

    orchestrator = AudioOrchestrator(config)

    await orchestrator.start()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
