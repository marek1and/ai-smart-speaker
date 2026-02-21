"""ReSpeaker XVF3800 system delay tuning.

Routine (command names match xvf_host exactly):
  1. AUDIO_MGR_OP_L 3 0, AUDIO_MGR_OP_R 5 0 (L=mic, R=ref).
  2. AUDIO_MGR_SYS_DELAY (read current).
  3. Record 2ch 16kHz (L=mic, R=ref) while playing startup.wav.
  4. Estimate sample offset between L and R via cross-correlation.
  5. AUDIO_MGR_SYS_DELAY = current + (measured_offset - buffer); ref must be buffer samples ahead of mic.
  6. AUDIO_MGR_OP_L 6 3, AUDIO_MGR_OP_R 6 3 (restore).
  7. SAVE_CONFIGURATION 1.

Can be run standalone (__main__) or imported and called from the app.
"""

from __future__ import annotations

import argparse
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import sounddevice as sd
import soundfile as sf

from .xvf_client import open_respeaker

logger = logging.getLogger(__name__)

# Paths: tools/ is at project root, so sounds/ is parent / "sounds"
SOUNDS_DIR = Path(__file__).resolve().parent.parent / "sounds"
STARTUP_WAV = SOUNDS_DIR / "startup.wav"

# xvf_host: AUDIO_MGR_OP_L / AUDIO_MGR_OP_R (2× uint8)
# Tuning: L = mic 0, R = reference; after tuning restore to (6, 3)
AUDIO_MGR_OP_L_TUNE = (3, 0)
AUDIO_MGR_OP_R_TUNE = (5, 0)
AUDIO_MGR_OP_L_RESTORE = (6, 3)
AUDIO_MGR_OP_R_RESTORE = (6, 3)

# Delay buffer so reference is slightly ahead of mic (in samples)
DEFAULT_BUFFER_SAMPLES = 20
# Allowed AUDIO_MGR_SYS_DELAY range (samples at 16 kHz)
SYS_DELAY_MIN = 80
SYS_DELAY_MAX = 160

RATE = 16000
CHANNELS = 2


@dataclass
class DelayTuneConfig:
    """Configuration for delay tuning."""

    startup_wav_path: Path = field(default_factory=lambda: STARTUP_WAV)
    """Path to startup.wav used as reference playback."""

    buffer_samples: int = DEFAULT_BUFFER_SAMPLES
    """Samples to leave so reference is slightly ahead of mic."""

    record_extra_seconds: float = 0.5
    """Extra recording time after playback ends."""

    correlation_max_lag_samples: int = 4000
    """Max lag to search (≈0.25 s at 16 kHz)."""

    dry_run: bool = False
    """If True, do not write SYS_DELAY or SAVE_CONFIGURATION."""

    recording_output_dir: Optional[Path] = None
    """Directory to save recording WAV (delay_adjustment_DATE_TIME_OFFSET.wav). None = recordings/ (same as session recordings)."""


def _load_startup_wav(path: Path, target_sr: int = RATE) -> tuple[np.ndarray, int]:
    """Load WAV and return (samples mono, target_sr); resampled if needed."""
    data, sr = sf.read(path, dtype="float32")
    if data.ndim == 2:
        data = np.mean(data, axis=1)
    if sr != target_sr:
        from scipy import signal as scipy_signal
        num = int(len(data) * target_sr / sr)
        data = scipy_signal.resample(data, num).astype(np.float32)
    return data, target_sr


def _record_while_playing(
    audio_play: np.ndarray,
    sample_rate: int,
    extra_seconds: float,
) -> np.ndarray:
    """Record 2ch (L=mic, R=ref) at 16kHz while playing audio_play. Uses default system devices."""
    recorded: list[np.ndarray] = []
    recording_done = threading.Event()

    def input_callback(indata: np.ndarray, _frames: int, _time_info: dict, status: sd.CallbackFlags) -> None:
        if status:
            logger.warning("Input status: %s", status)
        if not recording_done.is_set():
            recorded.append(indata.copy())

    duration_sec = len(audio_play) / sample_rate + extra_seconds
    stream_in = sd.InputStream(
        samplerate=RATE,
        channels=CHANNELS,
        dtype=np.float32,
        device=None,
        callback=input_callback,
        blocksize=1024,
    )
    stream_in.start()
    try:
        sd.play(audio_play, sample_rate, device=None)
        time.sleep(duration_sec)
        sd.stop()
    finally:
        recording_done.set()
        time.sleep(0.2)
        stream_in.stop()
        stream_in.close()

    if not recorded:
        raise RuntimeError("No recording captured")
    return np.concatenate(recorded, axis=0)


def _estimate_delay_samples(mic: np.ndarray, ref: np.ndarray, max_lag: int) -> int:
    """Estimate delay (samples) of mic relative to ref. Positive = mic is delayed.

    Uses cross-correlation; ref is reference, mic is delayed copy.
    Returns lag in range [-max_lag, max_lag]; we need positive lag (mic behind ref).
    """
    # Normalize
    mic = (mic - np.mean(mic)) / (np.std(mic) + 1e-12)
    ref = (ref - np.mean(ref)) / (np.std(ref) + 1e-12)
    # Cross-correlation: correlate(mic, ref)[k] = sum_n mic[n] ref[n-k]
    # When mic[n] ≈ ref[n - D], peak at k = D (ref shifted right by D aligns with mic)
    corr = np.correlate(mic, ref, mode="full")
    len_ref = len(ref)
    # lags: corr index 0 -> lag = -(len_ref-1); index len_mic+len_ref-2 -> lag = len_mic-1
    # so lag at index i = i - (len_ref - 1)
    lag_start = max(0, (len_ref - 1) - max_lag)
    lag_end = min(len(corr), (len_ref - 1) + max_lag + 1)
    search = corr[lag_start:lag_end]
    best_local = int(np.argmax(search))
    best_idx = lag_start + best_local
    lag_samples = best_idx - (len_ref - 1)
    return lag_samples


def tune_respeaker_system_delay(config: Optional[DelayTuneConfig] = None) -> int:
    """Run full delay tuning and return the new SYS_DELAY value (samples).

    Raises:
        FileNotFoundError: startup.wav not found.
        RuntimeError: Recording or ReSpeaker USB failed.
    """
    cfg = config or DelayTuneConfig()

    if not cfg.startup_wav_path.exists():
        raise FileNotFoundError(f"startup.wav not found: {cfg.startup_wav_path}")

    rs = open_respeaker()
    try:
        # 1. AUDIO_MGR_OP_L 3 0, AUDIO_MGR_OP_R 5 0 (L=mic, R=ref)
        logger.info("AUDIO_MGR_OP_L 3 0, AUDIO_MGR_OP_R 5 0")
        rs.write("AUDIO_MGR_OP_L", list(AUDIO_MGR_OP_L_TUNE))
        time.sleep(0.1)
        rs.write("AUDIO_MGR_OP_R", list(AUDIO_MGR_OP_R_TUNE))
        time.sleep(0.1)

        # 2. AUDIO_MGR_SYS_DELAY (read current)
        AUDIO_MGR_SYS_DELAY_current = rs.read_int32("AUDIO_MGR_SYS_DELAY")
        logger.info("AUDIO_MGR_SYS_DELAY (current): %d samples", AUDIO_MGR_SYS_DELAY_current)

        # 3–5. Record while playing startup.wav
        logger.info("Loading %s...", cfg.startup_wav_path)
        try:
            audio_play, play_sr = _load_startup_wav(cfg.startup_wav_path, target_sr=RATE)
        except ImportError:
            data, play_sr = sf.read(cfg.startup_wav_path, dtype="float32")
            if data.ndim == 2:
                data = np.mean(data, axis=1)
            if play_sr != RATE:
                logger.warning("Resampling requires scipy; playing at %d Hz (recording stays %d Hz)", play_sr, RATE)
            audio_play = data.astype(np.float32)
        logger.info("Recording 2ch @ %d Hz while playing startup.wav (%.2f s)...", RATE, len(audio_play) / play_sr)
        recorded = _record_while_playing(audio_play, play_sr, cfg.record_extra_seconds)
        mic = recorded[:, 0]
        ref = recorded[:, 1]

        # 4. Measured offset (lag = how many samples mic is behind ref)
        measured_offset = _estimate_delay_samples(mic, ref, cfg.correlation_max_lag_samples)
        logger.info("Measured offset (mic behind ref): %d samples", measured_offset)

        # Dump recording to WAV: delay_adjustment_DATE_TIME_OFFSET.wav
        out_dir = cfg.recording_output_dir if cfg.recording_output_dir is not None else Path("recordings")
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        date_time = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        wav_basename = f"delay_adjustment_{date_time}_{measured_offset}.wav"
        wav_path = out_dir / wav_basename
        recorded_int16 = (np.clip(recorded, -1.0, 1.0) * 32767).astype(np.int16)
        sf.write(wav_path, recorded_int16, RATE, subtype="PCM_16")
        logger.info("Saved recording: %s", wav_path)

        # 5. AUDIO_MGR_SYS_DELAY: account for current delay; ref must end up buffer_samples ahead of mic
        new_delay = AUDIO_MGR_SYS_DELAY_current + (measured_offset - cfg.buffer_samples)
        new_delay = max(SYS_DELAY_MIN, min(SYS_DELAY_MAX, new_delay))
        logger.info(
            "AUDIO_MGR_SYS_DELAY: current=%d + (offset=%d - buffer=%d) -> %d samples",
            AUDIO_MGR_SYS_DELAY_current, measured_offset, cfg.buffer_samples, new_delay,
        )

        if not cfg.dry_run:
            rs.write("AUDIO_MGR_SYS_DELAY", [new_delay])
            time.sleep(0.1)

        # Always restore routing to 6 3 on both channels (leave tune mode)
        rs.write("AUDIO_MGR_OP_L", list(AUDIO_MGR_OP_L_RESTORE))
        rs.write("AUDIO_MGR_OP_R", list(AUDIO_MGR_OP_R_RESTORE))
        time.sleep(0.1)
        logger.info("AUDIO_MGR_OP_L 6 3, AUDIO_MGR_OP_R 6 3 (restored)")

        if not cfg.dry_run:
            rs.write("SAVE_CONFIGURATION", [1])
            logger.info("SAVE_CONFIGURATION 1 done.")
        else:
            logger.info("Dry run: SYS_DELAY and SAVE_CONFIGURATION not written")

        return new_delay
    finally:
        rs.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(
        description="Tune ReSpeaker XVF3800 system delay using startup.wav (record L=mic, R=ref, correlate, set SYS_DELAY)."
    )
    parser.add_argument(
        "--startup-wav",
        type=Path,
        default=STARTUP_WAV,
        help=f"Path to startup.wav (default: {STARTUP_WAV})",
    )
    parser.add_argument(
        "--buffer-samples",
        type=int,
        default=DEFAULT_BUFFER_SAMPLES,
        help="Samples to leave so ref is ahead of mic (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not write SYS_DELAY or SAVE_CONFIGURATION",
    )
    parser.add_argument(
        "--recording-dir",
        type=Path,
        default=Path("recordings"),
        help="Directory for delay_adjustment_DATE_TIME_OFFSET.wav (default: recordings/)",
    )
    args = parser.parse_args()

    config = DelayTuneConfig(
        startup_wav_path=args.startup_wav,
        buffer_samples=args.buffer_samples,
        dry_run=args.dry_run,
        recording_output_dir=args.recording_dir,
    )
    try:
        new_delay = tune_respeaker_system_delay(config)
        print(f"Done. New SYS_DELAY = {new_delay} samples")
        return 0
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        logger.exception("%s", e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
