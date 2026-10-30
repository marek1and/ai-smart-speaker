# reSpeaker mixer levels as they were on the Raspberry Pi OS speaker: master
# (PCM Playback Volume, index 1) at 55 = -5 dB. The stock file is a dummy, so
# the card came up at its default of -20 dB; 0 dB clips after the AIC3104's
# 8 dB. Restored by 90-alsa-restore.rules whenever the card appears.
FILESEXTRAPATHS:prepend := "${THISDIR}/${PN}:"
