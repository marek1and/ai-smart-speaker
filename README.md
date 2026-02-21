# AI Smart Speaker: Edifier D12 Retrofit

This project is a high-end hardware retrofit of the **Edifier D12** 70W Bluetooth speaker, transforming it into a powerful, extensible AI voice assistant. By bypassing the stock connectivity and integrating a **Raspberry Pi 5** with a dedicated **DAC+**, this modification creates a "Best-in-Class" smart speaker platform for real-time LLM interaction (Gemini/OpenAI).

## 📝 Table of Contents

- [Safety Warning & Disclaimer](#-safety-warning--disclaimer)
- [Project Philosophy](#-project-philosophy)
- [Architectural Choice: Real-time API vs. Traditional STT/TTS](#-architectural-choice-real-time-api-vs-traditional-stttts)
- [Hardware Architecture](#️-hardware-architecture)
- [Hardware Implementation](#️-hardware-implementation)
- [System Configuration](#️-system-configuration)
- [Future Improvements](#-future-improvements)

## ⚠️ Safety Warning & Disclaimer

**DANGER: High Voltage.** This project involves modifying mains-powered equipment and installing an internal power supply unit (PSU) connected to AC lines.

- **Risk of Electric Shock:** Improper handling of high-voltage components can result in serious injury or death.
- **Fire Hazard:** Incorrect wiring or component selection can cause fire.
- **Warranty Void:** Opening the Edifier D12 enclosure will void its warranty.

**The author of this project accepts no responsibility for any damage to equipment, personal injury, or property damage resulting from the replication of this project.** Proceed only if you have the appropriate knowledge and experience with high-voltage electronics. Always disconnect power before working on the device.

## 💡 Project Philosophy

This is a hobby project driven by two primary goals:

1. **To Learn:** To explore the process of building a high-quality, end-to-end voice assistant from the ground up, covering hardware integration, low-level Linux audio configuration, and real-time application development.
2. **To Use:** To create a practical home assistant that supports the **Polish language**—a feature still lacking in many commercial smart speakers. The aim is to build a device that is not only functional but also a permanent, useful part of a smart home ecosystem.

The project prioritizes audio quality, low latency, and a modular software design that allows for future expansion.

## ⚖️ Architectural Choice: Real-time API vs. Traditional STT/TTS

Most open-source voice assistant projects, like the excellent [Rhasspy](https://rhasspy.readthedocs.io/), follow a standard offline pipeline:
`Wake Word -> Speech-to-Text (STT) -> Intent Handling / LLM -> Text-to-Speech (TTS)`

This project takes a different approach by leveraging **Real-time Multimodal APIs** (e.g., Gemini's Live API or OpenAI's Realtime API). In this model, raw audio is streamed directly to the API, which handles VAD, STT, LLM interaction, and TTS in a single, continuous session.

### Why a Real-time API?

- **Simplicity & Speed:** It eliminates the "pipeline latency" that accumulates at each step of a traditional STT-LLM-TTS flow. The round-trip time is significantly lower, resulting in a more natural, conversational feel.
- **End-to-End AI:** The entire interaction is managed by a single, powerful model, which can lead to more context-aware and human-like responses. The model can even begin generating a response before the user has finished speaking.
- **Focus on Hardware/Integration:** By offloading the core AI tasks, this project can focus on the hardware build, audio stack optimization, and creating a reliable platform.

This architecture trades the privacy and offline capabilities of traditional systems for state-of-the-art speed and conversational quality, which is ideal for a home assistant connected to a trusted network.

## 🎛️ Hardware Architecture

The core of this project is the integration of industrial-grade power and high-fidelity audio components into the original MDF enclosure.

### Bill of Materials (BOM)

- **Base Unit:** Edifier D12 Stereo Speaker (used for its high-quality drivers and enclosure).

- **Processing:** Raspberry Pi 5 (8GB) with Raspberry Pi Active Cooler.
- **Audio Output:** Raspberry Pi DAC+ (formerly IQaudIO) for audiophile-grade I2S output.
- **Voice Capture:** reSpeaker XVF3800 USB Microphone Array (Hardware-based VAD, AEC, and Beamforming).
- **Power Delivery:** Mean Well RS-25-5 Industrial Switching Power Supply (25W, 5V, 5A).
- **Connectivity:**
  - Shielded Cat 6a RJ45 Panel Mount (External access to Ethernet).
  - Industrial Metal USB 3.0 Type-A Panel Mount (External access to RPi ports).
  - Premium internal RCA and USB interconnects.

---

## 🛠️ Hardware Implementation

The modification focuses on internalizing the compute stack while maintaining the acoustic integrity of the Edifier chassis.

### 📸 Build Documentation

Photos of the assembly process are located in `hardware/pictures/`. The logical sequence is:

1. **`01-enclosure-disassembled.jpg`**: Internal layout assessment.
2. **`02-internal-psu-mount-point.jpg`** & **`03-internal-psu-mount-point.jpg`**: Preparing bracketry.
3. **`04-internal-psu-installed.jpg`**: Mounting the Mean Well PSU and routing AC lines.
4. **`05-rpi-dac-plus-stack.jpg`** through **`07-rpi-dac-plus-stack.jpg`**: Assembly of the RPi 5 + DAC+ stack.
5. **`08-rpi-mounted-in-chassis.jpg`** & **`09-rpi-mounted-in-chassis.jpg`**: Final placement near the panel mounts.
6. **`10-original-pcb-with-additional-wiring.jpg`** & **`11-original-pcb-with-additional-wiring.jpg`**: Bridging the Edifier amplifier inputs with the RPi DAC+ output.
7. **`12-enclosure-reassembly.jpg`** & **`13-enclosure-reassembly.jpg`**: Final internal cable management.
8. **`14-final-assembly-complete.jpg`**: Finished front-facing drivers.
9. **`15-top-view.jpg`** & **`16-rear-view.jpg`**: Final external appearance and panel mount access.

---

## ⚙️ System Configuration

The Raspberry Pi 5 runs a modern Linux audio stack optimized for low-latency voice processing. For details on the Python application, see the [Application README](./application/README.md).

### Audio Stack: PipeWire & WirePlumber

The system uses **PipeWire** to manage audio routing. This allows for seamless handling of the high-resolution DAC output and the reSpeaker microphone input.

- **DAC+ Setup:** Configured via `dtoverlay=rpi-dacplus` in `/boot/firmware/config.txt`.
- **WirePlumber:** Custom rules are used to prioritize the DAC+ for system-wide playback and the reSpeaker for capture.

### ReSpeaker XVF3800 Configuration & AEC Challenges

The USB mic array provides onboard DSP for Acoustic Echo Cancellation (AEC), which is crucial for allowing the assistant to hear its wake word while it is speaking (barge-in). However, this hardware-based AEC presented a significant challenge:

- **The Limitation:** The reSpeaker's hardware AEC only supports a 16kHz, mono playback reference signal. This is excellent for voice but results in poor audio quality for music or other high-fidelity sounds.
- **The Workaround:** To achieve both high-quality output and effective AEC, a hybrid audio path was created. The main audio output is routed through the high-fidelity DAC+, while a secondary, silent reference signal is sent to the reSpeaker's reference input.
- **The Catch:** This solution is critically dependent on a precisely tuned **system delay** (`AUDIO_MGR_SYS_DELAY`) on the reSpeaker. This delay aligns the audio being played from the DAC+ with the audio being captured by the microphones. The `tools/respeaker_delay_tune.py` script was developed to automate this calibration.

Even with a fixed system clock configured in PipeWire, this delay can drift slightly over hours of operation. This remains an area for potential improvement.

## 🚀 Future Improvements

This project is an ongoing effort. Planned future enhancements include:

- **Smart Home Integration:** Adding integration with [OpenHAB](https://www.openhab.org/) to control smart devices (lights, switches, etc.) via voice commands.
- **Music Streaming:** Integrating music streaming services like Spotify or YouTube Music.
- **Automatic Delay Calibration:** Implementing a background process to monitor the audio delay between the DAC and the reSpeaker in real-time and automatically adjust the `AUDIO_MGR_SYS_DELAY` to prevent drift.
- **Custom Linux Distribution:** Building a dedicated, minimal Linux distribution for the speaker using the [Yocto Project](https://www.yoctoproject.org/) to optimize boot time and performance.
