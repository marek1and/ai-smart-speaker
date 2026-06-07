# AI Smart Speaker: Edifier D12 Retrofit

This project is a hardware retrofit of the **Edifier D12** 70W Bluetooth speaker, transforming it into an AI voice assistant. A **Raspberry Pi 5** running a real-time API client (Gemini/OpenAI) is installed inside the original enclosure, paired with a **reSpeaker XVF3800** microphone array that handles audio output, hardware beamforming, and acoustic echo cancellation (AEC).

## 📝 Table of Contents

- [Safety Warning & Disclaimer](#️-safety-warning--disclaimer)
- [Project Philosophy](#-project-philosophy)
- [Architectural Choice: Real-time API vs. Traditional STT/TTS](#️-architectural-choice-real-time-api-vs-traditional-stttts)
- [Hardware Architecture](#️-hardware-architecture)
- [Hardware Evolution: v1 DAC+ to v2 XVF3800-only](#hardware-evolution-v1-dac-to-v2-xvf3800-only)
- [Hardware Implementation](#️-hardware-implementation)
- [System Configuration](#️-system-configuration)
- [Future Improvements](#future-improvements)

## ⚠️ Safety Warning & Disclaimer

**DANGER: High Voltage.** This project involves modifying mains-powered equipment and installing an internal power supply unit (PSU) connected to AC lines.

- **Risk of Electric Shock:** Improper handling of high-voltage components can result in serious injury or death.
- **Fire Hazard:** Incorrect wiring or component selection can cause fire.
- **Warranty Void:** Opening the Edifier D12 enclosure will void its warranty.

**The author of this project accepts no responsibility for any damage to equipment, personal injury, or property damage resulting from the replication of this project.** Proceed only if you have the appropriate knowledge and experience with high-voltage electronics. Always disconnect power before working on the device.

## 💡 Project Philosophy

This is a hobby project driven by two primary goals:

1. **To Learn:** To explore the process of building a high-quality, end-to-end voice assistant from the ground up, covering hardware integration, low-level Linux audio configuration, and real-time application development.
2. **To Use:** To create a practical home assistant that supports the **Polish language** — a feature still lacking in many commercial smart speakers. The aim is to build a device that is not only functional but also a permanent, useful part of a smart home ecosystem.

The project prioritizes audio quality, low latency, and a modular software design that allows for future expansion.

## ✨ Key Features

- **Single-device Audio:** The reSpeaker XVF3800 (firmware ≥ 2.0.9, 48kHz) handles both audio output in decent quality and microphone capture, eliminating the need for a separate DAC.
- **Hardware AEC:** The XVF3800's on-chip AEC cancels speaker echo using its internal reference — no software loopback required.
- **Real-time Conversational AI:** Leverages the native streaming APIs from **Google Gemini** and **OpenAI** for low-latency, natural-feeling conversations.
- **Robust Voice Capture:** The XVF3800 DSP provides hardware beamforming and AEC. Wake word detection runs in the application via [openWakeWord](https://github.com/dscripka/openWakeWord) (ONNX model, custom-trainable).
- **Smart Home Control via Function Calling:** Integrates seamlessly with **OpenHAB** to control smart devices like lights, switches, and sensors.
- **Multi-lingual:** While designed for Polish, the system can be configured for any language supported by the chosen AI provider.
- **Operational Monitoring:** Built-in Prometheus metrics endpoint with a pre-configured Grafana dashboard.

## ⚖️ Architectural Choice: Real-time API vs. Traditional STT/TTS

Most open-source voice assistant projects follow a standard offline pipeline:
`Wake Word -> Speech-to-Text (STT) -> Intent Handling / LLM -> Text-to-Speech (TTS)`

This project takes a different approach by leveraging **Real-time Multimodal APIs** (e.g., Gemini's Live API or OpenAI's Realtime API). Raw audio is streamed directly to the API, which handles VAD, STT, LLM interaction, and TTS in a single, continuous session.

### Why a Real-time API?

- **Simplicity & Speed:** Eliminates the "pipeline latency" that accumulates at each step of a traditional flow. The round-trip time is significantly lower, resulting in a more natural, conversational feel.
- **End-to-End AI:** The entire interaction is managed by a single, powerful model, leading to more context-aware and human-like responses.
- **Focus on Hardware/Integration:** By offloading core AI tasks, this project can focus on the hardware build, audio stack optimization, and creating a reliable platform.

This architecture trades the privacy and offline capabilities of traditional systems for state-of-the-art speed and conversational quality.

## 🎛️ Hardware Architecture

### Bill of Materials (BOM)

- **Base Unit:** Edifier D12 Stereo Speaker
- **Processing:** Raspberry Pi 5 (8GB) with Active Cooler
- **Audio I/O:** reSpeaker XVF3800 USB Microphone Array (speaker output + 4-mic array with hardware beamforming and AEC; firmware 2.0.9+ required for 48kHz operation)
- **Power Delivery:** Mean Well RS-25-5 Industrial Switching Power Supply (25W, 5V, 5A)
- **Connectivity:**
  - Shielded Cat 6a RJ45 Panel Mount
  - Industrial Metal USB 3.0 Type-A Panel Mount
  - Premium internal RCA and USB interconnects

> **Note:** The original v1 build included a **Raspberry Pi DAC+** for audio output. This has been superseded — see [Hardware Evolution](#hardware-evolution-v1-dac--v2-xvf3800-only) below.

---

## Hardware Evolution: v1 DAC+ to v2 XVF3800-only

The hardware and software architecture went through a significant simplification when Seeed Studio released firmware **2.0.9** for the XVF3800, adding native 48kHz USB audio support.

### v1 Architecture (XVF3800 firmware < 2.0.9, 16kHz only)

The original firmware only supported 16kHz on its USB audio interface. This forced a split audio path:

```text
Music / TTS  ──→  PipeWire combine-sink ──┬──→  I2S  ──→  DAC+  ──→  Edifier amplifier  ──→  speaker
                                          └──→  USB  ──→  XVF3800 (16kHz reference for AEC)
                                                          └──→  USB capture  ──→  application
```

This created several challenges:

- **Two clock domains:** DAC+ at 48kHz and XVF3800 at 16kHz required PipeWire to maintain a fixed quantum to prevent clock drift.
- **Software AEC reference loopback:** A PipeWire `combine-stream` sink had to continuously feed a downsampled (16kHz) copy of the played audio back to the XVF3800's USB playback input as the AEC reference signal.
- **Precise delay calibration:** Because the reference and playback took different paths, `AUDIO_MGR_SYS_DELAY` had to be precisely calibrated (within the ±5ms range of the parameter) to align the AEC reference with the acoustic echo. The `tools/respeaker_delay_tune.py` script was developed for this purpose.
- **Clock drift:** Even with a fixed PipeWire clock, the two-device setup could develop minor drift over long sessions.

### v2 Architecture (XVF3800 firmware 2.0.9+, 48kHz)

With firmware 2.0.9, the XVF3800 operates as a native 48kHz USB audio device for both playback and capture. The entire audio path is unified:

```text
Music / TTS  ──→  PipeWire  ──→  USB  ──→  XVF3800  ──→  speaker
                                           │  (internal AEC: playback → mic reference)
                                           └──→  USB capture  ──→  application
```

**What changed:**

- The DAC+ and the PipeWire combine-sink are no longer needed.
- The XVF3800's DSP handles AEC internally — it uses its own USB playback output as the AEC reference, without any software loopback.
- There is only one clock domain. PipeWire's fixed quantum is still configured (480 samples @ 48kHz = 10ms) for consistent low-latency scheduling, but clock drift between devices is no longer a concern.
- `AUDIO_MGR_SYS_DELAY` only needs to compensate for the chip-internal acoustic path (speaker → mic), which is fixed and small (~10 samples). The AEC adaptive filter handles the bulk of the pipeline latency automatically.

The trade-off is that audio quality is now determined by the XVF3800's built-in speaker amplifier rather than a dedicated DAC. For a voice assistant the audio quality is decent and entirely fit for purpose.

---

## 🛠️ Hardware Implementation

The modification focuses on internalizing the compute stack while maintaining the acoustic integrity of the Edifier chassis.

### 📸 Build Documentation

Photos of the assembly process are located in `hardware/pictures/`. The logical sequence is:

1. **`01-enclosure-disassembled.jpg`**: Internal layout assessment.
2. **`02-internal-psu-mount-point.jpg`** & **`03-internal-psu-mount-point.jpg`**: Preparing bracketry.
3. **`04-internal-psu-installed.jpg`**: Mounting the Mean Well PSU and routing AC lines.
4. **`05-rpi-dac-plus-stack.jpg`** through **`07-rpi-dac-plus-stack.jpg`**: Assembly of the RPi 5 + DAC+ stack (v1 build).
5. **`08-rpi-mounted-in-chassis.jpg`** & **`09-rpi-mounted-in-chassis.jpg`**: Final placement near the panel mounts.
6. **`10-original-pcb-with-additional-wiring.jpg`** & **`11-original-pcb-with-additional-wiring.jpg`**: Bridging the Edifier amplifier inputs with the RPi audio output.
7. **`12-enclosure-reassembly.jpg`** & **`13-enclosure-reassembly.jpg`**: Final internal cable management.
8. **`14-final-assembly-complete.jpg`**: Finished front-facing drivers.
9. **`15-top-view.jpg`** & **`16-rear-view.jpg`**: Final external appearance and panel mount access.

---

## ⚙️ System Configuration

The Raspberry Pi 5 runs a modern Linux audio stack optimized for low-latency voice processing. For details on the Python application, see the [Application README](./application/README.md).

### Autostart Configuration (systemd)

To configure the application to start automatically on boot as a user service:

1. Copy the provided systemd service file:

   ```bash
   mkdir -p ~/.config/systemd/user
   cp linux/home/user/.config/systemd/user/ai-smart-speaker.service ~/.config/systemd/user/
   ```

2. Enable lingering (allows user services to start on boot without login):

   ```bash
   loginctl enable-linger $USER
   ```

3. Reload and enable:

   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now ai-smart-speaker.service
   ```

4. View logs:

   ```bash
   journalctl --user-unit ai-smart-speaker -f
   ```

### Audio Stack: PipeWire & WirePlumber

The system uses **PipeWire** with the following configuration files (all under `linux/`):

| File | Purpose |
| --- | --- |
| `home/user/.config/pipewire/pipewire.conf.d/50-fixed-clock.conf` | Pins PipeWire to 48kHz, fixed quantum 480 (10ms) for consistent scheduling |
| `home/user/.config/wireplumber/wireplumber.conf.d/51-lowlatency-alsa.conf` | Keeps the XVF3800 always active (the application continuously reads mic audio for wake word detection) and tunes ALSA buffer |
| `home/user/.config/systemd/user/pipewire.service.d/rt.conf` | Grants PipeWire the `RLIMIT_RTPRIO` needed for real-time scheduling |
| `etc/polkit-1/rules.d/50-rtkit-pipewire.rules` | Allows RTKit to promote PipeWire's audio thread to `SCHED_RR` on headless systems |

### ReSpeaker XVF3800 Configuration

XVF3800 parameters are **saved to the chip's flash** via `SAVE_CONFIGURATION` and persist across reboots without any boot-time script. The udev rule only sets USB permissions.

After a firmware upgrade (which clears flash), re-apply settings manually:

```bash
/opt/reSpeaker/xvf_host -e /opt/reSpeaker/init_commands.txt
/opt/reSpeaker/xvf_host SAVE_CONFIGURATION 1
```

Key parameters (see `linux/opt/reSpeaker/init_commands.txt` for the full list):

| Parameter | Value | Purpose |
| --- | --- | --- |
| `AUDIO_MGR_MIC_GAIN` | 90 | Pre-beamformer microphone gain |
| `AUDIO_MGR_REF_GAIN` | 8 | Far-end reference gain for AEC |
| `AUDIO_MGR_SYS_DELAY` | 12 | Chip-internal acoustic path delay (samples) |
| `PP_AGCONOFF` | 1 | Automatic Gain Control enabled |
| `PP_ECHOONOFF` | 1 | Echo suppression enabled |
| `PP_NLATTENONOFF` | 1 | Non-linear echo attenuation enabled |

The AEC converges automatically via its adaptive filter. `AUDIO_MGR_SYS_DELAY` only fine-tunes the chip-internal path and has a valid range of `[-64, 256]` samples.

## Future Improvements

- **Music Streaming:** Integrating music streaming services (Spotify, YouTube Music).
- **Custom Linux Distribution:** Building a minimal Linux distribution using the [Yocto Project](https://www.yoctoproject.org/) to optimize boot time and performance.
