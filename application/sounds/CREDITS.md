# Credits and Acknowledgments

This project utilizes third-party audio assets to enhance the user experience. While the source code is licensed under MIT, the assets listed below maintain their original licenses (Public Domain).

## 🔊 Audio Assets

The interface sound effects (UI SFX) used in this project were created by **CogFireStudios** and sourced from [Freesound.org](https://freesound.org).

**License:** [CC0 1.0 Universal (Public Domain)](https://creativecommons.org/publicdomain/zero/1.0/)

| Project Filename | Description | Original Source |
| :--- | :--- | :--- |
| `wake_up.wav` | Trigger / Listening started | [Freesound #636650](https://freesound.org/people/CogFireStudios/sounds/636650/) |
| `follow_up.wav` | Active listening / Acknowledge | [Freesound #636677](https://freesound.org/people/CogFireStudios/sounds/636677/) |
| `end_conversation.wav` | Session closed / Idle | [Freesound #636647](https://freesound.org/people/CogFireStudios/sounds/636647/) |
| `confirm_action.wav` | Action confirmation | [Freesound #531508](https://freesound.org/people/CogFireStudios/sounds/531508/) |
| `error.wav` | System error / Network issue | [Freesound #636643](https://freesound.org/people/CogFireStudios/sounds/636643/) |
| `startup.wav` | System boot sequence (Remix) | Composite of [#619840](https://freesound.org/people/CogFireStudios/sounds/619840/) & [#619838](https://freesound.org/people/CogFireStudios/sounds/619838/) |

### Modifications

The original audio files have been processed for optimal performance on the target hardware (Raspberry Pi / ReSpeaker):

* **Resampling:** Converted to 24kHz / 16-bit PCM to match the audio output stream.
* **Normalization:** Volume levels adjusted (-10dB) to prevent clipping and match voice synthesis levels.
* **Mixing:** `startup.wav` is a custom mix of two separate stems to create a unique boot sequence.

---

*Special thanks to the open-source community and creators like CogFireStudios for making high-quality assets available to developers.*
