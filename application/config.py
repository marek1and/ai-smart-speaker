import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

# Default system instruction (in English)
DEFAULT_SYSTEM_INSTRUCTION = (
    "You are a voice assistant for a smart home. "
    "Your name is Alexa, and you should respond in a female voice. "
    "You understand and speak only English, unless explicitly asked to translate. "
    "You are versatile: you can control the house and answer general knowledge questions. "
    "If the audio signal is unclear or noisy, do not guess words and try to find meaning in the whole sentence. "
    "Ignore background noise (children, TV) and focus on the nearest adult voice. "
    "Respond concisely, in one sentence, unless asked for a detailed explanation."
)


@dataclass
class AudioConfig:
    """Audio input configuration."""

    # Input settings (microphone)
    input_sample_rate: int = 16000
    input_channels: int = 2  # Stereo input from mic (will extract mono)
    input_chunk: int = (
        512  # Frames per buffer (32ms at 16kHz, matches Silero VAD frame size)
    )
    input_device: Optional[str] = None  # Device name or None for default

    # Output settings (speaker)
    output_sample_rate: int = 24000  # Both Gemini and OpenAI return 24kHz audio
    output_channels: int = 1
    output_device: Optional[str] = None  # Device name or None for default

    # Queue sizes
    input_queue_maxsize: int = 50
    output_queue_maxsize: int = 500  # Large buffer for smooth audio playback


@dataclass
class WakeWordConfig:
    """Wake word detection configuration."""

    model_id: str = "alexa"  # Available: alexa, hey_mycroft, hey_jarvis
    threshold: float = 0.65  # Increased to reduce false positives from TV/music
    window_seconds: float = 0.8
    cooldown_seconds: float = 2.5  # Prevent re-triggers after detection


@dataclass
class VADConfig:
    """Voice Activity Detection configuration."""

    # VAD mode: "silero" (recommended, ONNX-based) or "rms" (simple energy)
    mode: str = "silero"

    # Silero VAD settings
    silero_threshold: float = 0.6  # Probability threshold for speech (0.0-1.0)
    silero_frame_size: int = 512  # Frame size for Silero (512 samples @ 16kHz = 32ms)

    # RMS VAD settings (fallback)
    rms_threshold: float = 1500.0  # RMS energy threshold

    # Temporal smoothing (for HybridVAD)
    speech_frames_required: int = 3  # Consecutive speech frames to start
    silence_frames_required: int = 15  # Consecutive silence frames to end (~500ms)


@dataclass
class LiveConfig:
    """Realtime API configuration (supports Gemini and OpenAI providers)."""

    # --- Provider selection ---
    provider: str = "gemini"  # "gemini" or "openai"

    # --- Common settings ---
    system_instruction: str = DEFAULT_SYSTEM_INSTRUCTION

    # Generation settings
    temperature: float = 0.2  # Lower = more deterministic, higher = more creative

    # Response modality - AUDIO only (enforced single modality)
    response_modalities: Sequence[str] = ("AUDIO",)

    lang: str = "en-US"

    # Audio format settings
    send_sample_rate: int = 16000
    receive_sample_rate: int = 24000
    input_mime_type: str = "audio/pcm;rate=16000"

    # Preroll: capture audio before wake word (0.8s captures full "Alexa")
    preroll_seconds: float = 0.8

    # Barge-in (interruption) settings
    barge_in: bool = True

    # Session management
    session_inactivity_timeout: float = 30.0  # Close session after N seconds idle
    max_reconnect_attempts: int = 3

    # Follow-up conversation
    followup_timeout: float = 3.0  # Seconds to wait for follow-up after AI response

    # --- Manual VAD Control (Hybrid Strategy) ---
    enable_manual_vad: bool = True  # Use local Silero VAD for speech boundaries

    # API VAD settings (failsafe with long timeout)
    api_vad_timeout: float = 2.0  # API-side silence timeout (failsafe)

    # Local VAD threshold (from VADConfig, duplicated for convenience)
    vad_threshold: float = 0.5  # Silero threshold for local VAD

    # --- Gemini-specific settings ---
    model: str = "gemini-2.5-flash-native-audio-preview-12-2025"
    voice_name: str = (
        "Zephyr"  # Gemini voice (Aoede, Charon, Fenrir, Kore, Puck, Zephyr)
    )

    # --- OpenAI-specific settings ---
    openai_model: str = "gpt-realtime"
    openai_voice: str = "shimmer"  # OpenAI voice (alloy, ash, ballad, coral, echo, sage, shimmer, verse)


@dataclass
class SoundConfig:
    """Sound effects configuration."""

    enabled: bool = True  # Enable/disable all sound effects
    volume: float = 1.0  # Volume level (0.0 - 1.0)
    sounds_dir: Optional[Path] = None  # Custom sounds directory (None = default)
    output_device: Optional[str] = None  # Audio device for sounds (None = default)


@dataclass
class ApiKeys:
    """API keys for the different services."""

    google_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None


@dataclass
class AppConfig:
    """Root application configuration."""

    audio: AudioConfig = field(default_factory=AudioConfig)
    vad: VADConfig = field(default_factory=VADConfig)
    wake_word: WakeWordConfig = field(default_factory=WakeWordConfig)
    live: LiveConfig = field(default_factory=LiveConfig)
    sound: SoundConfig = field(default_factory=SoundConfig)
    api_keys: ApiKeys = field(default_factory=ApiKeys)

    @classmethod
    def from_yaml(cls, path: str = "config.yml") -> "AppConfig":
        """Loads configuration from a YAML file, with fallbacks to defaults."""
        config_data = {}
        if Path(path).exists():
            with open(path, "r") as f:
                config_data = yaml.safe_load(f)

        # Create nested dataclasses from the loaded YAML data
        return cls(
            audio=AudioConfig(**config_data.get("audio", {})),
            vad=VADConfig(**config_data.get("vad", {})),
            wake_word=WakeWordConfig(**config_data.get("wake_word", {})),
            live=LiveConfig(**config_data.get("live", {})),
            sound=SoundConfig(**config_data.get("sound", {})),
            api_keys=ApiKeys(**config_data.get("api_keys", {})),
        )
