"""What of the model's audio is voice, and what of it is played."""

import logging
from dataclasses import dataclass, field

import numpy as np

from config import LiveConfig

logger = logging.getLogger(__name__)

_PCM16_FULL_SCALE = 32768.0
_PCM16_BYTES_PER_SAMPLE = 2


@dataclass
class ReplyChunk:
    """Verdict on one chunk of the reply."""

    seconds: float
    rms: float
    silent: bool = False
    # Voice the transcript does not account for: not played.
    over_budget: bool = False
    # What goes to the speaker now: a held pause, then the chunk itself.
    play: list[bytes] = field(default_factory=list)


class ReplyAudio:
    """Follows the audio of the reply to one turn, chunk by chunk.

    Silent chunks are held back until the model speaks again (then they play:
    a pause) or the reply ends (then they were trailing padding: up to 20 s of
    it on the speaker, 01.11.2026). Voice beyond what the output transcript
    accounts for is dropped. Knows nothing about the API or the turn's state.
    """

    def __init__(self, live_cfg: LiveConfig, sample_rate: int) -> None:
        self._live_cfg = live_cfg
        self._sample_rate = sample_rate
        self.chunks = 0
        self.silent_run = 0  # consecutive silent chunks
        self.held: list[bytes] = []
        self.held_bytes = 0
        self.ended_early = False  # the turn already ended before turn_complete
        self.voice_seen = False  # the model said something this turn
        self.voice_played = 0.0  # seconds of voice queued for the speaker
        self.voice_dropped = 0.0  # seconds of voice over the transcript budget

    def seconds(self, nbytes: int) -> float:
        """Duration of PCM16 audio at the output rate."""
        return nbytes / _PCM16_BYTES_PER_SAMPLE / self._sample_rate

    @staticmethod
    def rms(data: bytes) -> float:
        """Normalised RMS [0.0, 1.0] of a 16-bit little-endian PCM buffer."""
        arr = np.frombuffer(data, dtype=np.int16).astype(np.float32)
        return float(np.sqrt(np.mean(np.square(arr)))) / _PCM16_FULL_SCALE

    def is_silent(self, data: bytes) -> bool:
        """No voice in this chunk. Judged on the first difference (a high-pass):
        Gemini's broken answers carry a rumble below 300 Hz at RMS up to 0.017
        that plain RMS took for voice (6 s of it played on 12.11.2026)."""
        arr = np.frombuffer(data, dtype=np.int16).astype(np.float32)
        if arr.size < 2:
            return True
        level = float(np.sqrt(np.mean(np.square(np.diff(arr))))) / _PCM16_FULL_SCALE
        return level < self._live_cfg.output_silence_highpass_threshold

    def take(self, data: bytes, transcript_chars: int) -> ReplyChunk:
        """Takes the next chunk; transcript_chars is the length of the output
        transcript so far."""
        self.chunks += 1
        chunk = ReplyChunk(seconds=self.seconds(len(data)), rms=self.rms(data))
        chunk.silent = self.is_silent(data)
        self._log_silence_run(chunk)
        if chunk.silent:
            self.held.append(data)
            self.held_bytes += len(data)
            return chunk
        if self._over_voice_budget(chunk.seconds, transcript_chars):
            chunk.over_budget = True
            return chunk
        if self.ended_early:
            # Measures how often trailing_silence_end cuts an answer.
            logger.warning("Speech after the turn already ended early")
        self.voice_seen = True
        chunk.play = self._release_held() + [data]
        self.voice_played += chunk.seconds
        return chunk

    def drop_held(self) -> float:
        """Trailing silence: not played. Returns how many seconds it was."""
        trimmed = self.seconds(self.held_bytes)
        self.held, self.held_bytes = [], 0
        return trimmed

    def trailing_silence_is_long(self) -> bool:
        """The silence held since the model last spoke reached
        trailing_silence_end: once Gemini generated 76 s of it and sent
        generation_complete only after 12 s (01.11.2026)."""
        limit = self._live_cfg.trailing_silence_end
        if not limit or not self.voice_seen:
            return False  # disabled, or leading silence
        return self.seconds(self.held_bytes) >= limit

    def is_incomplete(self, transcript_chars: int) -> bool:
        """Too little voice for the transcript: Gemini sent 1.0 s of voice and
        12 s of silence for a 35-character sentence (09.11.2026). Normal
        answers take 0.06-0.11 s per character."""
        per_char = self._live_cfg.min_voice_per_char
        if not per_char or not transcript_chars:
            return False
        return self.voice_played < transcript_chars * per_char

    def _release_held(self) -> list[bytes]:
        """Speech follows: the held silence was a pause. It plays, cut to
        max_played_pause (6 s of silence came before a 0.1 s blip, 01.11.2026)."""
        budget = (
            int(self._live_cfg.max_played_pause * self._sample_rate)
            * _PCM16_BYTES_PER_SAMPLE
        )
        pause = []
        for held in self.held:
            if budget <= 0:
                break
            pause.append(held[:budget])
            budget -= len(held)
        self.held, self.held_bytes = [], 0
        return pause

    def _over_voice_budget(self, seconds: float, transcript_chars: int) -> bool:
        """Is this voice chunk more than the output transcript accounts for?
        Gemini once sent 51 s of audio for "Włączam Polsat." (03.11.2026);
        normal answers take up to 0.10 s per character."""
        per_char = self._live_cfg.voice_budget_per_char
        if not per_char or not transcript_chars:
            return False  # disabled, or no transcript to judge by
        budget = self._live_cfg.voice_budget_slack + transcript_chars * per_char
        if self.voice_played + seconds <= budget:
            return False
        if not self.voice_dropped:
            logger.warning(
                "Voice past %.1fs for a %d-character transcript — not played",
                budget, transcript_chars,
            )
        self.voice_dropped += seconds
        return True

    def _log_silence_run(self, chunk: ReplyChunk) -> None:
        if chunk.silent:
            self.silent_run += 1
            if self._silence_run_is_notable():
                logger.warning(
                    "Receiving SILENT audio (chunk #%d, rms=%.5f, consecutive_silent=%d) — watchdog NOT reset",
                    self.chunks, chunk.rms, self.silent_run,
                )
            else:
                logger.debug(
                    "Silent chunk #%d (rms=%.5f, consecutive_silent=%d)",
                    self.chunks, chunk.rms, self.silent_run,
                )
            return
        if self.silent_run > 0:
            log = (
                logger.info
                if self.silent_run >= self._live_cfg.output_silence_warn_after
                else logger.debug
            )
            log("Audio resumed after %d silent chunks (rms=%.5f)", self.silent_run, chunk.rms)
        self.silent_run = 0

    def _silence_run_is_notable(self) -> bool:
        """Warns once the run reaches output_silence_warn_after, then every
        output_silence_warn_every chunks for as long as the silence continues."""
        warn_after = self._live_cfg.output_silence_warn_after
        warn_every = self._live_cfg.output_silence_warn_every
        if self.silent_run < warn_after:
            return False
        if self.silent_run == warn_after:
            return True
        return warn_every > 0 and (self.silent_run - warn_after) % warn_every == 0
