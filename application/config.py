import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence
from google.genai.types import Modality

# Default system instruction (in English)
DEFAULT_SYSTEM_INSTRUCTION = (
    "You are a voice assistant for a smart home. "
    "Your name is Alexa, and you should respond in a female voice. "
    "You understand and speak only English, unless explicitly asked to translate. "
    "You are versatile: you can control the house and answer general knowledge questions. "
    "If the audio signal is unclear or noisy, do not guess words and try to find meaning in the whole sentence. "
    "Ignore background noise (children, TV) and focus on the nearest adult voice. "
    "Respond concisely, in one sentence, unless asked for a detailed explanation."
    "For any radio-related requests, like 'turn on radio', 'turn off radio', or asking about the status, your first step should always be to call the `get_radio_status` function to understand the current state. "
    "Based on the status: "
    "- If the user asks to 'turn on radio' and `is_radio_on_playlist` is true, call `play_internet_radio` without parameters to resume playback. "
    "- If `is_radio_on_playlist` is false, ask the user for a station name. "
    "- If the user asks to 'turn off radio' and the `playback_state` is 'play', call the `stop_radio` function. "
    "- If a specific station name is provided, use the `play_internet_radio` function with the `station_name` parameter."
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

    model_id: str = "alexa"  # Built-in: alexa, hey_mycroft, hey_jarvis, hey_rhasspy
    model_path: Optional[str] = (
        None  # Path to custom .onnx model file (overrides model_id when set)
    )
    threshold: float = 0.8
    vad_threshold: float = 0.5  # Silero VAD gate inside openWakeWord: scores zeroed on non-speech frames (0 = disabled)
    window_seconds: float = 0.8
    cooldown_seconds: float = 2.5  # Prevent re-triggers after detection
    min_activation_frames: int = (
        2  # Consecutive frames above threshold required to trigger
    )

    # Custom verifier model (sklearn .pkl loaded independently from openwakeword)
    verifier_path: Optional[str] = None  # Path to .pkl verifier
    # Wake word passes only when: model_score >= threshold AND verifier_score >= verifier_threshold
    verifier_threshold: float = 0.7

    # STT post-trigger verification (reduces false positives via faster-whisper)
    verify_with_stt: bool = False
    stt_model: str = "tiny.en"  # faster-whisper model: tiny.en, base.en, small.en
    stt_keywords: list[str] = field(
        default_factory=lambda: ["alexa", "alexia", "aleksa", "lexa"]
    )


@dataclass
class VADConfig:
    """Voice Activity Detection configuration."""

    # VAD mode: "silero" (recommended, ONNX-based) or "rms" (simple energy)
    mode: str = "silero"

    # Silero VAD settings
    silero_threshold: float = 0.7  # Probability threshold for speech (0.0-1.0)
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
    response_modalities: Sequence[Modality] = (Modality.AUDIO,)

    lang: str = "en-US"

    # Audio format settings
    send_sample_rate: int = 16000
    receive_sample_rate: int = 24000
    input_mime_type: str = "audio/pcm;rate=16000"

    # Preroll: capture audio before wake word (1.5s captures full "Alexa" + context)
    preroll_seconds: float = 1.5

    # Barge-in (interruption) settings
    barge_in: bool = True

    # Session management
    session_inactivity_timeout: float = 10.0  # Close session after N seconds idle
    session_max_turns: int = (
        4  # Close session after N completed turns (fresh context on next wake)
    )
    max_reconnect_attempts: int = 3

    # Follow-up conversation
    followup_timeout: float = 5.0  # Seconds to wait for follow-up after AI response

    # Initial silence timeout: close session if no speech detected after wake word
    # Protects against false wake-word triggers (e.g., nearby TV or conversations)
    initial_silence_timeout: float = 5.0

    # --- Manual VAD Control (Hybrid Strategy) ---
    enable_manual_vad: bool = True  # Use local Silero VAD for speech boundaries

    # API VAD settings (failsafe with long timeout)
    api_vad_timeout: float = 2.0  # API-side silence timeout (failsafe)
    turn_watchdog_timeout: float = 5.0  # Watchdog timeout for turn completion
    audio_done_watchdog_timeout: float = 3.0  # Tighter watchdog after audio stream ends (OpenAI response.output_audio.done)

    # Output silence detection: RMS threshold below which a received audio chunk is
    # considered silence. Silent chunks are forwarded to the speaker but do NOT reset
    # the turn watchdog — preventing Gemini from hanging the session by streaming
    # silence indefinitely instead of sending turn_complete.
    output_silence_rms_threshold: float = 0.002

    # Max seconds to wait for the speaker queue to drain after turn_complete.
    # Guards against AudioOutput hanging (sounddevice underrun / device error).
    speaker_drain_timeout: float = 10.0

    # --- Gemini-specific settings ---
    model: str = "gemini-3.1-flash-live-preview"
    voice_name: str = (
        "Zephyr"  # Gemini voice (Aoede, Charon, Fenrir, Kore, Puck, Zephyr)
    )

    # --- OpenAI-specific settings ---
    openai_model: str = "gpt-realtime"
    openai_voice: str = "marin"  # OpenAI voice (marin, cedar, alloy, ash, ballad, coral, echo, sage, shimmer, verse)


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
class OpenHabConfig:
    """OpenHab integration configuration."""

    url: str = "http://localhost:8080"
    api_key: Optional[str] = None


@dataclass
class StationPin:
    uuid: str
    name: str = ""


@dataclass
class RadioConfig:
    """Internet radio configuration."""

    country: str = "Poland"
    stations: dict = field(default_factory=dict)


@dataclass
class TVConfig:
    """Samsung TV control configuration."""

    power_item: str = "GF_LivingRoom_TV_Power"
    channel_item: str = "GF_LivingRoom_TV_Channel"
    boot_wait_timeout: float = 5.0  # Max seconds to wait for TV to confirm ON
    boot_poll_interval: float = 1.0  # How often to poll power state after cold start
    post_boot_delay: float = (
        2.0  # Extra seconds to wait after TV confirms ON before sending channel
    )
    # channel name (as user says it) -> channel number
    channels: dict = field(default_factory=dict)


@dataclass
class MPDConfig:
    """MPD server configuration."""

    host: str = "localhost"
    port: int = 6600
    connection_timeout: int = 5
    volume_fade_in_seconds: float = 2.0  # Duration of volume fade-in (resuming)
    volume_duck_percentage: int = 20  # Volume percentage during conversation
    default_playback_volume: int = (
        50  # Default playback volume if no previous volume is known
    )
    state_file: str = "radio_state.json"


@dataclass
class MQTTConfig:
    """MQTT broker configuration for OpenHAB integration."""

    enabled: bool = False
    broker: str = "localhost"
    port: int = 1883
    client_id: str = "ai-smart-speaker"
    topic_prefix: str = "home/living-room/speaker"
    username: Optional[str] = None
    password: Optional[str] = None
    reconnect_interval: float = 5.0


@dataclass
class MetricsConfig:
    """Prometheus metrics HTTP server configuration."""

    enabled: bool = True
    port: int = 9090


def _parse_radio_config(data: dict) -> "RadioConfig":
    raw_stations = data.pop("stations", {})
    cfg = RadioConfig(**data)
    for keyword, pin in raw_stations.items():
        if isinstance(pin, dict):
            cfg.stations[keyword.lower()] = StationPin(**pin)
        elif isinstance(pin, str):
            cfg.stations[keyword.lower()] = StationPin(uuid=pin)
    return cfg


@dataclass
class AppConfig:
    """Root application configuration."""

    audio: AudioConfig = field(default_factory=AudioConfig)
    vad: VADConfig = field(default_factory=VADConfig)
    wake_word: WakeWordConfig = field(default_factory=WakeWordConfig)
    live: LiveConfig = field(default_factory=LiveConfig)
    sound: SoundConfig = field(default_factory=SoundConfig)
    api_keys: ApiKeys = field(default_factory=ApiKeys)
    openhab: OpenHabConfig = field(default_factory=OpenHabConfig)
    radio: RadioConfig = field(default_factory=RadioConfig)
    mpd: MPDConfig = field(default_factory=MPDConfig)
    tv: TVConfig = field(default_factory=TVConfig)
    mqtt: MQTTConfig = field(default_factory=MQTTConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)

    @classmethod
    def from_yaml(cls, path: str = "config.yml") -> "AppConfig":
        """Loads configuration from a YAML file, with fallbacks to defaults."""
        config_data = {}
        if Path(path).exists():
            with open(path, "r") as f:
                config_data = yaml.safe_load(f)

        tv = TVConfig(**config_data.get("tv", {}))
        radio = _parse_radio_config(config_data.get("radio", {}))

        live_data = dict(config_data.get("live", {}))
        if "system_instruction" in live_data:
            # Inject TV channel names
            if tv.channels:
                channel_names = ", ".join(tv.channels.keys())
                live_data["system_instruction"] = live_data[
                    "system_instruction"
                ].replace("{tv_channels}", channel_names)
            # Inject pinned station names so the model uses canonical names
            if radio.stations:
                station_list = ", ".join(pin.name for pin in radio.stations.values())
                live_data["system_instruction"] = live_data[
                    "system_instruction"
                ].replace("{radio_stations}", station_list)

        return cls(
            audio=AudioConfig(**config_data.get("audio", {})),
            vad=VADConfig(**config_data.get("vad", {})),
            wake_word=WakeWordConfig(**config_data.get("wake_word", {})),
            live=LiveConfig(**live_data),
            sound=SoundConfig(**config_data.get("sound", {})),
            api_keys=ApiKeys(**config_data.get("api_keys", {})),
            openhab=OpenHabConfig(**config_data.get("openhab", {})),
            radio=radio,
            mpd=MPDConfig(**config_data.get("mpd", {})),
            tv=tv,
            mqtt=MQTTConfig(**config_data.get("mqtt", {})),
            metrics=MetricsConfig(**config_data.get("metrics", {})),
        )
