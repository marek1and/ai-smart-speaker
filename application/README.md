# AI Smart Speaker Application

This directory contains the core Python application for the AI Smart Speaker. It's designed to run on a Linux-based system with access to the necessary audio hardware, such as a Raspberry Pi. The application uses a real-time API (either Gemini or OpenAI) to function as a voice assistant, featuring wake-word detection, voice activity detection (VAD), and a modular architecture that allows for easy extension and customization.

## Project Structure

The project is organized into the following directories:

- `audio/`: Contains modules for audio input/output, VAD, and wake-word detection.
- `functions/`: Handles the definition and registration of tools (functions) that the AI can call, such as controlling smart home devices.
- `openhab/`: Contains the client for interacting with the OpenHAB REST API.
- `realtime/`: Manages the real-time communication with the selected API (Gemini or OpenAI).
- `sounds/`: Stores sound effects for different events (e.g., wake word, end o`f` conversation).
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

## Tools

### Delay Tuning

The `tools/respeaker_delay_tune.py` script helps to tune the audio delay for the ReSpeaker microphone array.

To run the delay tuning tool, use the following command:

```bash
python -m tools.respeaker_delay_tune
```

**Options:**

- `--startup-wav`: Path to the `startup.wav` file. Defaults to the `sounds/startup.wav` file in the project directory.
- `--buffer-samples`: Samples to leave so the reference is ahead of the mic. Defaults to `20`.
- `--dry-run`: If set, the script will not write the new `SYS_DELAY` or save the configuration.
- `--recording-dir`: Directory to save the recording. Defaults to `recordings/`.

For example, to run the tool in dry-run mode and save the recording to a custom directory, you can use the following command:

```bash
python -m tools.respeaker_delay_tune --dry-run --recording-dir /path/to/recordings
```
