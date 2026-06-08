"""Prometheus metrics for the AI smart speaker."""

import os
import time
from pathlib import Path
from typing import Optional

import psutil
from prometheus_client import Counter, Gauge, Info, start_http_server

_APP_START_TIME = time.time()
_PROCESS = psutil.Process(os.getpid())
_CPU_TEMP_PATH = Path('/sys/class/thermal/thermal_zone0/temp')

# System
APP_UPTIME = Gauge('speaker_app_uptime_seconds', 'Application uptime in seconds')
SYSTEM_UPTIME = Gauge('speaker_system_uptime_seconds', 'System uptime in seconds')
PROCESS_CPU = Gauge('speaker_process_cpu_percent', 'Process CPU usage percent')
CPU_PER_CORE = Gauge('speaker_cpu_core_percent', 'System CPU usage per core', ['core'])
CPU_TEMP = Gauge('speaker_cpu_temp_celsius', 'CPU temperature in Celsius')
PROCESS_MEMORY = Gauge('speaker_process_memory_bytes', 'Process RSS memory in bytes')
SYSTEM_MEMORY_USED = Gauge('speaker_system_memory_used_bytes', 'System used memory in bytes')
SYSTEM_MEMORY_TOTAL = Gauge('speaker_system_memory_total_bytes', 'System total memory in bytes')

# Wake word
WAKE_DETECTIONS = Counter(
    'speaker_wake_word_detections_total', 'Wake word detections',
    ['context'],  # idle, barge_in, re_listen
)
FALSE_TRIGGERS = Counter(
    'speaker_false_triggers_total', 'False trigger events',
    ['reason'],  # initial_silence, stt_rejection
)

# State machine
STATE_TRANSITIONS = Counter(
    'speaker_state_transitions_total', 'State machine transitions',
    ['from_state', 'to_state'],
)
STATE_ACTIVE = Gauge(
    'speaker_state_active', 'Current FSM state (1 = active)',
    ['state'],
)

# Sessions
SESSIONS_OPENED = Counter('speaker_sessions_opened_total', 'API sessions opened')
SESSIONS_CLOSED = Counter(
    'speaker_sessions_closed_total', 'API sessions closed',
    ['reason'],  # max_turns, inactivity, false_trigger
)
SESSION_TURNS = Counter('speaker_session_turns_total', 'Conversation turns completed')
API_ERRORS = Counter('speaker_api_errors_total', 'API session errors')
TOKENS_INPUT = Counter(
    'speaker_tokens_input_total', 'Input tokens consumed',
    ['provider'],
)
TOKENS_OUTPUT = Counter(
    'speaker_tokens_output_total', 'Output tokens consumed',
    ['provider'],
)

# Conversation events
BARGE_INS = Counter('speaker_barge_ins_total', 'Barge-in interruptions executed')
FOLLOWUP_TIMEOUTS = Counter('speaker_followup_timeouts_total', 'Follow-up conversation timeouts')
FOLLOWUPS_STARTED = Counter('speaker_followups_started_total', 'Follow-up conversations started')

# Radio
RADIO_PLAYS = Counter(
    'speaker_radio_plays_total', 'Radio playback starts',
    ['source', 'station'],  # source: ai_new, ai_resume, mqtt_power, mqtt_station
)
RADIO_STOPS = Counter(
    'speaker_radio_stops_total', 'Radio playback stops',
    ['source'],  # ai, mqtt
)
RADIO_VOLUME_CHANGES = Counter(
    'speaker_radio_volume_changes_total', 'Radio volume change commands',
    ['source'],  # ai, mqtt
)
RADIO_DUCKS = Counter('speaker_radio_ducks_total', 'Radio volume duck events')
RADIO_UNDUCKS = Counter('speaker_radio_unducks_total', 'Radio volume unduck events')
RADIO_PLAYING = Gauge('speaker_radio_playing', 'Radio playback state (1=playing, 0=stopped)')
STATION_PLAY_COUNT = Gauge('speaker_station_play_count', 'Total play count per station (persisted across restarts)', ['station'])
RADIO_VOLUME = Gauge('speaker_radio_volume_percent', 'Radio restore volume percentage')
MPD_RECONNECTIONS = Counter('speaker_mpd_reconnections_total', 'MPD error-triggered reconnection events')

# MQTT
MQTT_COMMANDS = Counter(
    'speaker_mqtt_commands_total', 'MQTT commands received',
    ['command'],  # power_on, power_off, station, volume
)
MQTT_CONNECTED = Gauge('speaker_mqtt_connected', 'MQTT broker connection state (1=connected)')

# OpenHAB
OPENHAB_REQUESTS = Counter(
    'speaker_openhab_requests_total', 'OpenHAB REST API requests',
    ['method', 'status'],  # method: get/set — status: ok/error
)
OPENHAB_ITEM_SETS = Counter(
    'speaker_openhab_item_sets_total', 'OpenHAB item state changes',
    ['item'],
)
TV_COMMANDS = Counter(
    'speaker_tv_commands_total', 'TV control commands',
    ['action'],  # power_on, channel_switch
)

# AI tool calls
AI_TOOL_CALLS = Counter(
    'speaker_ai_tool_calls_total', 'AI-invoked tool function calls',
    ['function'],
)

# ReSpeaker XVF3800 hardware
RESPEAKER_AVAILABLE = Gauge('speaker_respeaker_available', 'ReSpeaker USB device available (1=yes)')
RESPEAKER_AEC_CONVERGED = Gauge('speaker_respeaker_aec_converged', 'AEC convergence (1=converged)')
RESPEAKER_AEC_PATH_CHANGE = Gauge('speaker_respeaker_aec_path_change', 'AEC path change flag (1=change detected)')
RESPEAKER_RT60 = Gauge('speaker_respeaker_rt60_seconds', 'Room RT60 reverberation estimate (seconds; negative=invalid)')
RESPEAKER_SPEECH_ENERGY = Gauge('speaker_respeaker_speech_energy', 'Speech energy per beam (>0 = speech)', ['beam'])
RESPEAKER_AGC_GAIN_DB = Gauge('speaker_respeaker_agc_gain_db', 'AGC current gain (dB)')
RESPEAKER_DOA_AZIMUTH = Gauge('speaker_respeaker_doa_azimuth_degrees', 'Direction of arrival azimuth (degrees)', ['source'])
RESPEAKER_AEC_HEADROOM_US = Gauge('speaker_respeaker_aec_headroom_us', 'AEC DSP current idle headroom (µs)')
RESPEAKER_PP_HEADROOM_US = Gauge('speaker_respeaker_pp_headroom_us', 'Post-processing DSP current idle headroom (µs)')

APP_INFO = Info('speaker_app', 'Application information')


def start(port: int, provider: str = 'unknown') -> None:
    start_http_server(port)
    APP_INFO.info({'provider': provider})
    _pre_register_labels()


def _pre_register_labels() -> None:
    """Touch all known label combos at 0 so Prometheus scrapes them before the first inc().
    Without this, increase() misses the first event (counter created and incremented
    in the same scrape interval, so Prometheus never sees the 0 → 1 transition)."""
    for ctx in ('idle', 'barge_in', 're_listen'):
        WAKE_DETECTIONS.labels(context=ctx)
    for reason in ('initial_silence', 'stt_rejection'):
        FALSE_TRIGGERS.labels(reason=reason)
    for reason in ('max_turns', 'inactivity', 'false_trigger'):
        SESSIONS_CLOSED.labels(reason=reason)
    for fn in ('play_internet_radio', 'stop_radio', 'set_playback_volume',
               'get_radio_status', 'set_openhab_item_state', 'watch_tv'):
        AI_TOOL_CALLS.labels(function=fn)
    for cmd in ('power_on', 'power_off', 'station', 'volume'):
        MQTT_COMMANDS.labels(command=cmd)
    for action in ('power_on', 'channel_switch'):
        TV_COMMANDS.labels(action=action)
    for method in ('get', 'set'):
        for status in ('ok', 'error'):
            OPENHAB_REQUESTS.labels(method=method, status=status)
    for source in ('ai', 'mqtt'):
        RADIO_STOPS.labels(source=source)
    for source in ('ai_new', 'ai_resume', 'mqtt_power', 'mqtt_station'):
        RADIO_PLAYS.labels(source=source, station='unknown')
    for beam in ('beam_1', 'beam_2', 'free_running', 'auto_select'):
        RESPEAKER_SPEECH_ENERGY.labels(beam=beam)
    for src in ('processed', 'auto_select'):
        RESPEAKER_DOA_AZIMUTH.labels(source=src)


def _read_cpu_temp() -> Optional[float]:
    try:
        return int(_CPU_TEMP_PATH.read_text()) / 1000.0
    except Exception:
        return None


def update_system_metrics() -> None:
    APP_UPTIME.set(time.time() - _APP_START_TIME)
    SYSTEM_UPTIME.set(time.time() - psutil.boot_time())
    PROCESS_CPU.set(_PROCESS.cpu_percent(interval=None))
    for i, pct in enumerate(psutil.cpu_percent(percpu=True)):
        CPU_PER_CORE.labels(core=str(i)).set(pct)
    temp = _read_cpu_temp()
    if temp is not None:
        CPU_TEMP.set(temp)
    PROCESS_MEMORY.set(_PROCESS.memory_info().rss)
    vm = psutil.virtual_memory()
    SYSTEM_MEMORY_USED.set(vm.used)
    SYSTEM_MEMORY_TOTAL.set(vm.total)
