"""Wake word detection using openwakeword (ONNX only)."""

import time
from collections import deque
from typing import Deque, Tuple

import numpy as np
from openwakeword.model import Model
from openwakeword.utils import download_models

from config import AudioConfig, WakeWordConfig

_MODELS_CACHE: dict[str, bool] = {}


def _ensure_models_downloaded() -> None:
    """Download openwakeword models if not already present."""
    if _MODELS_CACHE.get("downloaded"):
        return

    print("[INFO] Checking openwakeword models...")
    download_models()
    _MODELS_CACHE["downloaded"] = True


class WakeWordDetector:
    """Wake word detection using openwakeword with ONNX runtime."""

    def __init__(self, wake_cfg: WakeWordConfig, audio_cfg: AudioConfig) -> None:
        self.cfg = wake_cfg
        self.window_size = int(
            wake_cfg.window_seconds * audio_cfg.input_sample_rate / audio_cfg.input_chunk
        )
        self.score_window: Deque[float] = deque(maxlen=self.window_size)
        self._model_id = wake_cfg.model_id.replace(" ", "_")
        self._last_trigger_time: float = 0.0  # For cooldown
        # Counter for consecutive frames above threshold (false-positive suppression)
        self._consecutive_above_threshold: int = 0
        self.model = self._load_model()

    def _load_model(self) -> Model:
        """
        Load only the configured wake word model (ONNX only).

        Uses inference_framework='onnx' to avoid tflite dependency.
        Downloads models automatically if not present.
        """
        # Ensure models are downloaded
        _ensure_models_downloaded()

        # Load model by name - openwakeword will find the correct file
        # Available models: alexa, hey_mycroft, hey_jarvis, hey_rhasspy, timer, weather
        try:
            model = Model(
                wakeword_models=[self._model_id],
                inference_framework="onnx",
            )
        except Exception as e:
            available = ["alexa", "hey_mycroft", "hey_jarvis", "hey_rhasspy", "timer", "weather"]
            raise RuntimeError(
                f"Failed to load wake word model '{self._model_id}': {e}\n"
                f"Available models: {available}"
            ) from e

        # The model name includes version suffix (e.g., alexa_v0.1)
        # Store the actual key used in predictions
        self._prediction_key = list(model.models.keys())[0]

        print(f"[INFO] Loaded wake word model (ONNX): {self._prediction_key}")
        return model

    def process(self, mono: np.ndarray) -> Tuple[float, float, bool]:
        """
        Process audio frame and detect wake word.

        Args:
            mono: Mono audio samples as int16 numpy array

        Returns:
            Tuple of (current_score, max_window_score, triggered)
        """
        predictions = self.model.predict(mono)
        score = float(predictions.get(self._prediction_key, 0.0))

        self.score_window.append(score)
        max_score = max(self.score_window) if self.score_window else 0.0

        # Track consecutive frames above threshold to suppress brief false-positive spikes
        # (e.g., distant TV voices or conversations in another room)
        if score >= self.cfg.threshold:
            self._consecutive_above_threshold += 1
        else:
            self._consecutive_above_threshold = 0

        # Check cooldown - don't trigger if we triggered recently
        now = time.time()
        in_cooldown = (now - self._last_trigger_time) < self.cfg.cooldown_seconds

        # Require N consecutive frames above threshold before triggering
        triggered = (
            self._consecutive_above_threshold >= self.cfg.min_activation_frames
            and not in_cooldown
        )

        # Update last trigger time when triggered
        if triggered:
            self._last_trigger_time = now

        return score, max_score, triggered

    def reset(self) -> None:
        """Reset the model state and score window (cooldown timer preserved)."""
        self.model.reset()
        self.score_window.clear()
        self._consecutive_above_threshold = 0

    def set_cooldown(self) -> None:
        """Manually trigger cooldown (e.g., after conversation ends)."""
        self._last_trigger_time = time.time()
