"""Background task: poll ReSpeaker XVF3800 USB parameters and update Prometheus metrics."""

import asyncio
import logging
import math

import metrics
from tools.xvf_client import ReSpeaker

logger = logging.getLogger(__name__)

_BEAM_LABELS = ("beam_1", "beam_2", "free_running", "auto_select")
_TICKS_TO_US = 0.01  # 10 ns ticks → µs


async def respeaker_monitor_task(respeaker: ReSpeaker, interval: float = 5.0) -> None:
    """Poll XVF3800 parameters every `interval` seconds and update Prometheus metrics."""
    metrics.RESPEAKER_AVAILABLE.set(1)
    while True:
        await asyncio.sleep(interval)
        try:
            _poll(respeaker)
        except Exception as e:
            logger.warning("ReSpeaker metric poll failed: %s", e)
            metrics.RESPEAKER_AVAILABLE.set(0)


def _poll(respeaker: ReSpeaker) -> None:
    (converged,) = respeaker.read("AEC_AECCONVERGED")
    metrics.RESPEAKER_AEC_CONVERGED.set(converged)

    (path_change,) = respeaker.read("AEC_AECPATHCHANGE")
    metrics.RESPEAKER_AEC_PATH_CHANGE.set(path_change)

    (rt60,) = respeaker.read("AEC_RT60")
    # Device returns ~1.4e-45 (float32 denormal) when no active speech to estimate from.
    # Use NaN so Prometheus/Grafana shows "No data" rather than a misleading near-zero.
    metrics.RESPEAKER_RT60.set(rt60 if rt60 > 1e-10 else float("nan"))

    energies = respeaker.read("AEC_SPENERGY_VALUES")
    for label, val in zip(_BEAM_LABELS, energies):
        metrics.RESPEAKER_SPEECH_ENERGY.labels(beam=label).set(val)

    (agc_linear,) = respeaker.read("PP_AGCGAIN")
    agc_db = 20.0 * math.log10(agc_linear) if agc_linear > 0 else -120.0
    metrics.RESPEAKER_AGC_GAIN_DB.set(agc_db)

    azimuths = respeaker.read("AUDIO_MGR_SELECTED_AZIMUTHS")
    for src, val in zip(("processed", "auto_select"), azimuths):
        deg = math.degrees(val) if not math.isnan(val) else float("nan")
        metrics.RESPEAKER_DOA_AZIMUTH.labels(source=src).set(deg)

    (aec_idle,) = respeaker.read("AEC_CURRENT_IDLE_TIME")
    metrics.RESPEAKER_AEC_HEADROOM_US.set(aec_idle * _TICKS_TO_US)

    (pp_idle,) = respeaker.read("PP_CURRENT_IDLE_TIME")
    metrics.RESPEAKER_PP_HEADROOM_US.set(pp_idle * _TICKS_TO_US)

    metrics.RESPEAKER_AVAILABLE.set(1)
