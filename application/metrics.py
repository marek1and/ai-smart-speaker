"""Prometheus metrics for the AI smart speaker."""

import os
import time

import psutil
from prometheus_client import Counter, Gauge, Info, start_http_server

_APP_START_TIME = time.time()
_PROCESS = psutil.Process(os.getpid())

# System
APP_UPTIME = Gauge('speaker_app_uptime_seconds', 'Application uptime in seconds')
SYSTEM_UPTIME = Gauge('speaker_system_uptime_seconds', 'System uptime in seconds')
PROCESS_CPU = Gauge('speaker_process_cpu_percent', 'Process CPU usage percent')
PROCESS_MEMORY = Gauge('speaker_process_memory_bytes', 'Process RSS memory in bytes')

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

APP_INFO = Info('speaker_app', 'Application information')


def start(port: int, provider: str = 'unknown') -> None:
    start_http_server(port)
    APP_INFO.info({'provider': provider})


def update_system_metrics() -> None:
    APP_UPTIME.set(time.time() - _APP_START_TIME)
    SYSTEM_UPTIME.set(time.time() - psutil.boot_time())
    PROCESS_CPU.set(_PROCESS.cpu_percent(interval=None))
    PROCESS_MEMORY.set(_PROCESS.memory_info().rss)
