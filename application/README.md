# AI Smart Speaker Application

This directory contains the core Python application for the AI Smart Speaker. It's designed to run on a Linux-based system with access to the necessary audio hardware, such as a Raspberry Pi. The application uses a real-time API (either Gemini or OpenAI) to function as a voice assistant, featuring wake-word detection, voice activity detection (VAD), and a modular architecture that allows for easy extension and customization.

## Project Structure

The project is organized into the following directories:

- `audio/`: Contains modules for audio input/output, VAD, and wake-word detection.
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
