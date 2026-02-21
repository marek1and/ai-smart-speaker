#!/usr/bin/env python3
"""
AI Smart Speaker with Realtime API (Gemini / OpenAI).

Entry point for the voice assistant application.
"""

import asyncio
import logging

from config import AppConfig
from orchestrator import AudioOrchestrator

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

logger = logging.getLogger(__name__)


async def run() -> None:
    """Main entry point."""

    # Load configuration from YAML (with defaults)
    config = AppConfig.from_yaml()

    logger.info("Realtime provider: %s", config.live.provider)

    orchestrator = AudioOrchestrator(config)

    await orchestrator.start()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
