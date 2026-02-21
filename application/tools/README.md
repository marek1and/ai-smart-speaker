# Tools

## ReSpeaker XVF3800 delay tuning (`respeaker_delay_tune`)

Script for automatic system delay (AUDIO_MGR_SYS_DELAY) tuning on ReSpeaker XVF3800 USB 4-MIC Array. Talks to the device over USB via PyUSB (no external xvf_host.py). Uses default system input/output devices for recording and playback.

### Requirements

- ReSpeaker connected via USB (vid=0x2886, pid=0x001A). On Windows you may need `pip install libusb_package` for the device to be found.
- Default system microphone = ReSpeaker (2 ch, 16 kHz). Default playback = speaker (reference wired to same source).
- PyUSB (`pip install pyusb`).

### Standalone usage

From the project root, after installing dependencies (`pip install -r requirements.txt`):

```bash
python -m tools.respeaker_delay_tune [options]
```

Options:

- `--startup-wav PATH` – WAV file to play (default: `sounds/startup.wav`).
- `--buffer-samples 20` – samples so that reference is ~20 samples ahead of mic.
- `--dry-run` – only measure and print new delay, do not write to ReSpeaker.
- `--recording-dir PATH` – directory for recording file (default: `recordings/`, same as session recordings).

After each run, the 2ch recording (L=mic, R=ref) is saved as  
`delay_adjustment_DATE_TIME_OFFSET.wav` for manual verification.

### Use from the app

```python
from tools import DelayTuneConfig, tune_respeaker_system_delay

config = DelayTuneConfig(buffer_samples=20)
new_delay = tune_respeaker_system_delay(config)
```

Delay logic: `AUDIO_MGR_SYS_DELAY = current + (measured_offset - buffer_samples)`. Command names in code match xvf_host (AUDIO_MGR_OP_L, AUDIO_MGR_OP_R, AUDIO_MGR_SYS_DELAY, SAVE_CONFIGURATION).
