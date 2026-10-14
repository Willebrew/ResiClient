"""Serial reader that recovers from a stalled USB-serial link.

The TransCore reader sits behind a PL2303 USB-serial adapter. When the
adapter's USB read URB stops, the kernel logs
"pl2303 ttyUSB0: usb_serial_generic_read_bulk_callback - urb stopped: -32".
The tty stays open, no bytes arrive, and pyserial raises nothing, so the
read loop silently stops. Closing and reopening the port resubmits the read,
so this reader reopens the port after a stretch of silence and whenever a
read fails.
"""

import time
from typing import Callable, Optional

import serial

STALL_TIMEOUT = 300.0  # Reopen after this many seconds without any bytes
RETRY_INTERVAL = 5.0   # Seconds between attempts to open a missing port


class SerialReader:
    def __init__(
        self,
        port: str,
        baudrate: int,
        timeout: float,
        stall_timeout: float = STALL_TIMEOUT,
        retry_interval: float = RETRY_INTERVAL,
        opener: Callable = serial.Serial,
        clock: Callable[[], float] = time.monotonic,
        log: Callable[[str], None] = print,
    ) -> None:
        self._port = port
        self._baudrate = baudrate
        self._timeout = timeout
        self._stall_timeout = stall_timeout
        self._retry_interval = retry_interval
        self._opener = opener
        self._clock = clock
        self._log = log

        self._ser: Optional[serial.SerialBase] = None
        self._last_data = clock()
        self._next_open = 0.0
        self._failing = False
        self._try_open()

    @property
    def connected(self) -> bool:
        return self._ser is not None

    def readline(self) -> bytes:
        """Return one line, or b"" if nothing arrived within the read timeout."""
        if self._ser is None:
            if self._clock() >= self._next_open:
                self._try_open()
            return b""

        try:
            data = self._ser.readline()
        except (serial.SerialException, OSError) as exc:
            self._log(f"[SERIAL-RECOVER] read failed: {exc}; reopening {self._port}")
            self._close()
            self._try_open()
            return b""

        now = self._clock()
        if data:
            self._last_data = now
            return data

        silent_for = now - self._last_data
        if silent_for >= self._stall_timeout:
            self._log(f"[SERIAL-RECOVER] no data for {int(silent_for)}s; reopening {self._port}")
            self._close()
            self._try_open()
        return b""

    def close(self) -> None:
        self._close()

    def _try_open(self) -> None:
        try:
            ser = self._opener(
                port=self._port, baudrate=self._baudrate, timeout=self._timeout
            )
            ser.reset_input_buffer()
        except (serial.SerialException, OSError) as exc:
            self._ser = None
            self._next_open = self._clock() + self._retry_interval
            # Log the first failure of a streak, not every retry.
            if not self._failing:
                self._log(f"[SERIAL-ERROR] cannot open {self._port}: {exc}; retrying")
            self._failing = True
            return

        self._ser = ser
        self._last_data = self._clock()
        if self._failing:
            self._log(f"[SERIAL-RECOVER] {self._port} reopened")
        self._failing = False

    def _close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except (serial.SerialException, OSError):
                pass
        self._ser = None
