"""ReSpeaker XVF3800 USB control (minimal in-process, no xvf_host.py).

Uses PyUSB to talk to the device. Only the commands needed for delay tuning
are defined (same names as in xvf_host).
"""

from __future__ import annotations

import logging
import struct
import sys
import time
from typing import List, Union

logger = logging.getLogger(__name__)

# Optional: on Windows, libusb_package is often needed for PyUSB to find the device
try:
    import usb.core
    import usb.util
except ImportError as e:
    raise ImportError(
        "PyUSB is required for ReSpeaker control. Install with: pip install pyusb"
    ) from e

if sys.platform.startswith("win"):
    try:
        import libusb_package
    except ImportError:
        libusb_package = None
else:
    libusb_package = None

CONTROL_SUCCESS = 0
SERVICER_COMMAND_RETRY = 64
TIMEOUT_MS = 100000

# Commands used for delay tuning (name -> resid, cmdid, count, rw, type)
# Same names as xvf_host for 1:1 mapping
PARAMETERS = {
    "AUDIO_MGR_OP_L": (35, 15, 2, "rw", "uint8"),
    "AUDIO_MGR_OP_R": (35, 19, 2, "rw", "uint8"),
    "AUDIO_MGR_SYS_DELAY": (35, 26, 1, "rw", "int32"),
    "SAVE_CONFIGURATION": (48, 9, 1, "wo", "uint8"),
    "LED_EFFECT": (20, 12, 1, "rw", "uint8"),
    "LED_BRIGHTNESS": (20, 13, 1, "rw", "uint8"),
    "LED_SPEED": (20, 15, 1, "rw", "uint8"),
    "LED_COLOR": (20, 16, 1, "rw", "uint32"),
    "LED_DOA_COLOR": (20, 17, 2, "rw", "uint32"),
    "LED_RING_COLOR": (20, 19, 12, "rw", "uint32"),
}


def _find_device(vid: int = 0x2886, pid: int = 0x001A):
    """Find ReSpeaker XVF3800 USB device."""
    if libusb_package is not None:
        return libusb_package.find(idVendor=vid, idProduct=pid)
    return usb.core.find(idVendor=vid, idProduct=pid)


class ReSpeaker:
    """Minimal ReSpeaker control (write / read int32)."""

    def __init__(self, dev) -> None:
        self._dev = dev

    def write(self, name: str, data_list: List[Union[int, float]]) -> None:
        """Execute a write command (same param names as xvf_host)."""
        data = PARAMETERS.get(name)
        if not data:
            raise ValueError(f"Unknown command: {name}")
        if data[3] == "ro":
            raise ValueError(f"{name} is read-only")
        if len(data_list) != data[2]:
            raise ValueError(f"{name} expects {data[2]} values, got {len(data_list)}")

        windex, wvalue, data_cnt, _rw, data_type = data
        payload = _build_payload(data_type, data_cnt, data_list)

        self._dev.ctrl_transfer(
            usb.util.CTRL_OUT
            | usb.util.CTRL_TYPE_VENDOR
            | usb.util.CTRL_RECIPIENT_DEVICE,
            0,
            wvalue,
            windex,
            payload,
            TIMEOUT_MS,
        )

    def read_int32(self, name: str) -> int:
        """Read one int32 value (e.g. AUDIO_MGR_SYS_DELAY)."""
        data = PARAMETERS.get(name)
        if not data:
            raise ValueError(f"Unknown command: {name}")
        if data[3] == "wo":
            raise ValueError(f"{name} is write-only")
        if data[4] != "int32" or data[2] != 1:
            raise ValueError(f"{name} is not a single int32")

        windex, wvalue, data_cnt, _rw, data_type = data
        length = data_cnt * 4 + 1  # status + 4 bytes
        wvalue_read = 0x80 | wvalue

        read_attempts = 0
        while read_attempts <= 100:
            response = self._dev.ctrl_transfer(
                usb.util.CTRL_IN
                | usb.util.CTRL_TYPE_VENDOR
                | usb.util.CTRL_RECIPIENT_DEVICE,
                0,
                wvalue_read,
                windex,
                length,
                TIMEOUT_MS,
            )
            if response[0] == CONTROL_SUCCESS:
                break
            if response[0] == SERVICER_COMMAND_RETRY:
                read_attempts += 1
                time.sleep(0.01)
                continue
            raise RuntimeError(f"ReSpeaker {name} status: {response[0]}")
        else:
            raise RuntimeError("ReSpeaker read attempt limit exceeded")

        return struct.unpack("<i", bytes(response[1:5]))[0]

    def close(self) -> None:
        usb.util.dispose_resources(self._dev)


def _build_payload(
    data_type: str, data_cnt: int, data_list: List[Union[int, float]]
) -> bytes:
    payload = []
    if data_type == "uint8":
        for i in range(data_cnt):
            payload.append(int(data_list[i]).to_bytes(1, byteorder="little"))
    elif data_type == "int32":
        for i in range(data_cnt):
            payload.append(struct.pack("<i", int(data_list[i])))
    elif data_type == "uint32":
        for i in range(data_cnt):
            payload.append(struct.pack("<I", int(data_list[i])))
    else:
        raise ValueError(f"Unsupported type for write: {data_type}")
    return b"".join(payload)


def open_respeaker(vid: int = 0x2886, pid: int = 0x001A) -> ReSpeaker:
    """Find and open ReSpeaker. Caller must call .close() when done."""
    dev = _find_device(vid=vid, pid=pid)
    if dev is None:
        raise RuntimeError(
            "ReSpeaker not found (vid=0x2886, pid=0x001A). "
            "On Windows you may need: pip install libusb_package"
        )
    return ReSpeaker(dev)


class ReSpeakerLeds:
    """Manages ReSpeaker LED effects."""

    def __init__(self, respeaker: ReSpeaker) -> None:
        self._respeaker = respeaker

    def _set_color(
        self, effect: int, color: int = 0, speed: int = 1, brightness: int = 255
    ) -> None:
        try:
            self._respeaker.write("LED_EFFECT", [effect])
            if effect in (1, 3):  # Breath or Single Color
                self._respeaker.write("LED_COLOR", [color])
            if effect in (1, 2):  # Breath or Rainbow
                self._respeaker.write("LED_SPEED", [speed])
                self._respeaker.write("LED_BRIGHTNESS", [brightness])
        except Exception as e:
            logger.error("Failed to set ReSpeaker LEDs: %s", e)

    def set_idle(self) -> None:
        self._set_color(effect=0)

    def set_listening(self) -> None:
        """Breath mode, green — waiting for user speech."""
        self._set_color(effect=1, color=0x00FF00, speed=1, brightness=255)

    def set_responding(self) -> None:
        """Rainbow mode — AI is responding."""
        self._set_color(effect=2, speed=1, brightness=255)
