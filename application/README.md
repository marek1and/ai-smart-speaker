# AI Smart Speaker Application

This directory contains the core Python application for the AI Smart Speaker. It's designed to run on a Linux-based system with access to the necessary audio hardware, such as a Raspberry Pi. The application uses a real-time API (either Gemini or OpenAI) to function as a voice assistant, featuring wake-word detection, voice activity detection (VAD), and a modular architecture that allows for easy extension and customization.

## Project Structure

The project is organized into the following directories:

- `audio/`: Contains modules for audio input/output, VAD, and wake-word detection.
- `functions/`: Handles the definition and registration of tools (functions) that the AI can call, such as controlling smart home devices.
- `openhab/`: Contains the client for interacting with the OpenHAB REST API.
- `realtime/`: Manages the real-time communication with the selected API (Gemini or OpenAI).
- `sounds/`: Stores sound effects for different events (e.g., wake word, end of conversation).
- `tools/`: Includes utility scripts for tasks like tuning audio delay.
- `config.py`: Defines the data classes for the application's configuration.
- `main.py`: The main entry point for the application.
- `orchestrator.py`: The core module that orchestrates the different components of the application.
- `state.py`: Defines the different states of the application.
- `config.yml`: A user-configurable file for customizing the application's behavior.

## Setup

1. **Create and activate a virtual environment:**

    ```bash
    python -m venv .venv
    source .venv/bin/activate
    ```

2. **Install dependencies:**

    ```bash
    pip install -r requirements.txt
    ```

    *Note: On some systems, you might need to install `openwakeword` with `--no-deps`.*

3. **Configure the application:**
    Create a `config.yml` file in the root of the project and add your API keys and other custom configurations. See the `Configuration` section below for more details.

## Running the Application

To run the application, simply execute the `main.py` script:

```bash
python main.py
```

## Deployment to Raspberry Pi

The `sync-to-rpi.sh` script provides a convenient way to synchronize the application files to a Raspberry Pi. It uses `rsync` to efficiently transfer the project while excluding unnecessary files like virtual environments, cache, and local recordings.

**Usage:**

```bash
./sync-to-rpi.sh [hostname]
```

- `[hostname]` (optional): The hostname or IP address of your Raspberry Pi. Defaults to `raspberrypi`.

The script will sync the contents of the `application` directory to `~/ai-voice-speaker/` on the remote device.

## Configuration

The application is configured using the `config.yml` file. If this file does not exist, the application will use the default values defined in `config.py`.

To create a custom configuration, create a `config.yml` file in the root of the project and add the desired configuration values.

### API Keys

You can provide your API keys in the `config.yml` file:

```yaml
api_keys:
  google_api_key: "YOUR_GOOGLE_API_KEY"
  openai_api_key: "YOUR_OPENAI_API_KEY"
```

### Real-time Provider

The real-time provider can be configured in the `live` section of the `config.yml` file. The available providers are `gemini` and `openai`.

```yaml
live:
  provider: "gemini"
```

### System Instruction

You can also customize the system instruction in the `live` section:

```yaml
live:
  system_instruction: >
    You are a helpful assistant.
```

### Conversation Flow & User Input

To make the conversation feel natural and avoid the assistant waiting for user input when it's not expected, the assistant uses a special tool called `request_for_user_input`.

If you want the assistant to ask follow-up questions or wait for your response after it performs an action, you should explicitly instruct it in the system prompt to call the `request_for_user_input` function. For example:

- "If you need to ask the user for details or expect a response, ALWAYS call the `request_for_user_input` function."

This allows the AI to control the conversation flow, deciding when to keep listening and when to end the turn.

### Smart Home Integration (OpenHAB)

This application supports controlling smart home devices through an OpenHAB instance. This is achieved using the "function calling" or "tools" feature of the selected AI model (Gemini or OpenAI). The assistant can understand natural language commands (e.g., "turn on the living room light"), and the LLM will translate this into a call to the appropriate function to interact with the OpenHAB REST API.

#### 1. Configuration

First, you must configure the connection to your OpenHAB server in `config.yml`:

```yaml
openhab:
  # URL of your OpenHab instance
  url: "http://192.168.1.100:8080"
  # API key for OpenHab (if required)
  api_key: "YOUR_OPENHAB_API_KEY"
```

#### 2. Informing the Model About Your Devices

The most critical step is telling the LLM which devices are available to control. **The model does not know your devices automatically.** You must list them explicitly in the system prompt.

The `live.system_instruction` in your `config.yml` is where you provide this context. You should create a clear, structured list of your rooms and devices, including the exact `Item ID` that the function needs to use.

A detailed example of how to structure this prompt is provided in `config.yml.example`. It is highly recommended that you follow this template for best results.

**Example Snippet from `config.yml.example`:**

```yaml
live:
  system_instruction: |
    You are a voice assistant...
    Here is the complete list of available rooms and devices with their Item IDs that you must use:

    ### GROUND FLOOR (GF)

    **Living Room**
    *   **Lighting:**
        *   Main: `GF_LivingRoom_MainLight` (Switch On/Off)
        *   Dimmer: `GF_LivingRoom_Dimmer` (Dimming 0-100%)
    *   **Climate:**
        *   Temperature: `GF_LivingRoom_Temperature` (Read-only)

    ### OPERATING RULES:
    1. ...
```

By providing this detailed context, the model will know to call `set_openhab_item_state(item_name='GF_LivingRoom_MainLight', state='ON')` when you say "turn on the main light in the living room."

## TV Control

The assistant can turn on the TV and switch channels using a single `watch_tv` tool backed by OpenHAB items.

### TV Configuration

Add a `tv` section to `config.yml`. The `channels` map defines the names the model will accept — they are automatically injected into the system prompt via `{tv_channels}`:

```yaml
tv:
  # OpenHAB: power switch item name | Home Assistant: media_player entity ID
  power_item: "GF_LivingRoom_TV_Power"
  channel_item: "GF_LivingRoom_TV_Channel"  # OpenHAB only
  boot_wait_timeout: 20.0   # max seconds to poll for TV ON after cold start
  boot_poll_interval: 1.0
  post_boot_delay: 10.0     # seconds to wait after TV reports ON before switching channel
  channels:
    "BBC One": 101
    "BBC Two": 102
    "ITV": 103
```

**Home Assistant note:** many TV integrations (Samsung Tizen, LG webOS, Android TV via ADB) can't power on a fully-off TV over the network — the TV's Wi-Fi/Ethernet is asleep and won't respond until it sees a Wake-on-LAN magic packet. Some integrations send WOL automatically if you configure a MAC address in the integration itself; check your integration's options first. If yours doesn't, point `power_item` at a `switch.*` entity backed by HA's own `wake_on_lan` integration instead of the `media_player` entity — `watch_tv` will use it to power the TV on generically. Channel switching still requires a `media_player` entity, so it's unavailable in that setup until the TV is already on.

Reference the placeholder in your system instruction so the model knows exactly which names to pass:

```yaml
live:
  system_instruction: |
    ...
    Available TV channels (pass the exact name to watch_tv): {tv_channels}
```

### Cold Boot Handling

When the TV is off and a channel is requested, `watch_tv` sends the power-on command and returns immediately so the AI response is not delayed. A background task then polls the power state and sends the channel command once the TV confirms it is on (plus `post_boot_delay`). Tune `post_boot_delay` to match your TV's boot time — Samsung TVs typically need 8–12 seconds after reporting ON before they are ready to accept channel commands.

## Monitoring

The application exposes a Prometheus metrics endpoint for real-time operational monitoring. A Grafana dashboard is included for visualization.

### Metrics Endpoint

When enabled (default), the application starts an HTTP server on port `9090` that serves metrics in Prometheus format:

```text
http://<speaker-ip>:9090/metrics
```

Configure the port (or disable) in `config.yml`:

```yaml
metrics:
  enabled: true
  port: 9090
```

### What is Tracked

| Category | Metrics |
| --- | --- |
| **System** | App and system uptime, process CPU %, RAM usage |
| **Wake Word** | Detection count by context (idle / barge-in / re-listen), false trigger count by reason (initial silence / STT rejection) |
| **Sessions** | Sessions opened/closed (with close reason: max_turns / inactivity / false_trigger), turns completed, API errors |
| **Conversation** | Barge-ins, follow-ups started, follow-up timeouts, state machine transitions |
| **Radio** | Play events by source (AI new station / AI resume / MQTT power / MQTT station), stops, volume changes, duck/unduck events, current volume, MPD reconnections |
| **MQTT** | Connection state, command counts by type (power_on / power_off / station / volume) |
| **OpenHAB** | HTTP requests by method and status, per-item state change counts |
| **TV** | Power-on and channel-switch command counts |
| **AI Tools** | Per-function call counts (play_internet_radio, stop_radio, watch_tv, set_openhab_item_state, set_playback_volume) |

### Setting Up Grafana

A complete monitoring stack (Prometheus + Grafana) is provided in the `monitoring/` directory at the project root. The Grafana dashboard is pre-configured and loads automatically.

**Quick start:**

1. Edit `monitoring/prometheus.yml` and replace `SPEAKER_IP` with your speaker's IP address.
2. Start the stack:

   ```bash
   cd monitoring
   docker compose up -d
   ```

3. Open Grafana at `http://localhost:3000` (default login: `admin` / `admin`).

The dashboard loads automatically under **AI Smart Speaker → AI Smart Speaker**.

To change the Grafana admin password, set the `GRAFANA_PASSWORD` environment variable before starting:

```bash
GRAFANA_PASSWORD=mysecret docker compose up -d
```

Prometheus retains 90 days of data by default.

## Tools

### Delay Tuning (legacy — v1 DAC+ builds only)

The `tools/respeaker_delay_tune.py` script calibrates `AUDIO_MGR_SYS_DELAY` for setups where the XVF3800 plays audio through an **external DAC** (e.g., the original RPi DAC+ build). In that configuration the AEC reference and the speaker output travel different paths, so the delay must be measured and set precisely.

With firmware 2.0.9+ and the XVF3800 acting as both speaker and microphone, this tool is no longer needed — the chip handles the internal delay automatically.

```bash
python -m tools.respeaker_delay_tune --dry-run
```

## Internet Radio and MPD Integration

The assistant can play internet radio stations using the **Music Player Daemon (MPD)**, a flexible and powerful server for music playback.

### 1. MPD (Music Player Daemon) Setup

MPD is used as a dedicated and stable service to handle the audio playback of internet radio streams. This separates the playback from the main application logic, improving reliability.

**Installation (Debian/Raspberry Pi OS):**

```bash
sudo apt-get update
sudo apt-get install mpd
```

For this project, the application communicates with MPD over a local network connection. The default MPD configuration (`/etc/mpd.conf`) is usually sufficient if the application is running on the same device (e.g., the Raspberry Pi).

If you are running MPD on a different machine or need to customize its settings, you can specify the host and port in your `config.yml`:

```yaml
mpd:
  host: "localhost"
  port: 6600
```

### 2. Using the Radio Functionality

Once MPD is running, you can ask the assistant to play radio stations.

**How it Works:**

1. **User Command:** You ask the assistant to play a radio station (e.g., "Play RMF FM").
2. **Function Calling:** The LLM uses the `play_internet_radio` tool with the station name.
3. **Station Resolution:** The system resolves the stream URL using this priority chain:
   - **URL cache** in `radio_state.json` — instant, no network call.
   - **Pinned station UUID** — looked up via RadioBrowser API, URL cached for next time.
   - **RadioBrowser name search** — fallback for unpinned stations, result cached too.
4. **MPD Control:** The orchestrator tells MPD to load and play the resolved stream URL.

**Basic Configuration:**

```yaml
radio:
  country: "Poland"
```

**Pinned Stations:**

For popular stations, pin them by their stable [RadioBrowser](https://www.radio-browser.info) UUID. This avoids ambiguous search results and automatically refreshes the stream URL when a station migrates CDN.

```yaml
radio:
  country: "Poland"
  stations:
    rmf fm:
      uuid: "399b7c2a-6680-11e8-b15b-52543be04c81"
      name: "RMF FM"
    radio zet:
      uuid: "59e30dda-64bf-11ea-be63-52543be04c81"
      name: "Radio Zet"
```

The `name` field is what the assistant says aloud and what the LLM receives in the system prompt. The key (`rmf fm`) is matched case-insensitively against the user's query — both `"rmf fm"` and `"RMF FM"` resolve to the same pin. Matching also works by official name, so `"Radio Zet"` and `"radio zet"` both find the pin.

Pinned station names are automatically injected into the system prompt via `{radio_stations}`, so the model knows exactly which names to use as `station_name` parameter.

To find a station's UUID, search at [radio-browser.info](https://www.radio-browser.info) and copy the UUID from the station detail page.

**State persistence (`radio_state.json`):**

Resolved stream URLs are cached in `radio_state.json` (path configurable via `mpd.state_file`). The file also tracks the current station and per-station play counts used by Prometheus metrics. After the first successful UUID resolution, subsequent plays of the same station require no RadioBrowser API call.

**Audio Ducking:**

The system features automatic audio ducking. If the radio is playing and you say the wake word:

- The radio volume is immediately lowered to `mpd.volume_duck_percentage`.
- Once the conversation ends, the volume fades back to its previous level.
