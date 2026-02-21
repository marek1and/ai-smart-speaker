"""Realtime streaming API clients (Gemini, OpenAI, etc.)."""

import asyncio
import logging

from config import AppConfig
from realtime.base import BaseRealtimeManager
from realtime.gemini import GeminiRealtimeManager
from realtime.openai_rt import OpenAIRealtimeManager

logger = logging.getLogger(__name__)

__all__ = [
    "BaseRealtimeManager",
    "GeminiRealtimeManager",
    "OpenAIRealtimeManager",
    "create_realtime_manager",
]


def create_realtime_manager(
    config: "AppConfig",
    input_queue: asyncio.Queue,
    output_queue: asyncio.Queue,
) -> BaseRealtimeManager:
    """Factory function to create the appropriate realtime API manager.

    Args:
        config: Application configuration.
        input_queue: Async queue for audio input (mic -> API).
        output_queue: Async queue for audio output (API -> speaker).

    Returns:
        An instance of the appropriate realtime manager.

    Raises:
        ValueError: If the provider is not supported.
    """
    provider = config.live.provider.lower()

    if provider == "gemini":
        logger.info("Creating Gemini Realtime API manager")
        return GeminiRealtimeManager(config, input_queue, output_queue)
    elif provider == "openai":
        logger.info("Creating OpenAI Realtime API manager")
        return OpenAIRealtimeManager(config, input_queue, output_queue)
    else:
        raise ValueError(
            f"Unknown realtime provider: '{provider}'. "
            f"Supported providers: 'gemini', 'openai'"
        )
