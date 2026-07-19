#!/usr/bin/env python3
"""Low-latency relay control for the Denkovi USB board.

The normal Denkovi command line tool opens the USB device and starts a JVM for
every state change.  RelayDaemon keeps both alive and accepts pulse commands
over a private stdin/stdout pipe.  The original CLI remains as a reliability
fallback if the daemon cannot be started.
"""

import atexit
import os
import select
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional


BASE_DIR = Path(__file__).resolve().parent
JAR_PATH = Path(
    os.environ.get(
        "DENKOVI_CLI_JAR",
        "/home/admin/pi-transcore-access-control/DenkoviRelayCommandLineTool.jar",
    )
)
DENKOVI_LIB_PATH = Path(
    os.environ.get(
        "DENKOVI_HID_JAR",
        "/home/admin/pi-transcore-access-control/lib/denkoviHID-1.6-jar-with-dependencies.jar",
    )
)
DAEMON_CLASS_DIR = BASE_DIR / "relay_daemon"
DAEMON_SOURCE = DAEMON_CLASS_DIR / "RelayDaemon.java"
DAEMON_CLASS = DAEMON_CLASS_DIR / "RelayDaemon.class"
RELAY_SERIAL = os.environ.get("DENKOVI_RELAY_SERIAL", "0007252401")
DAEMON_START_TIMEOUT = 8.0
DAEMON_COMMAND_TIMEOUT = 3.0

RELAY_MAP = {
    "jones": 1,
    "harvey": 2,
}


class RelayDaemonClient:
    """Own and communicate with one persistent RelayDaemon JVM."""

    def __init__(self) -> None:
        self._process: Optional[subprocess.Popen[str]] = None
        self._lock = threading.Lock()

    def _readline(self, timeout: float) -> str:
        process = self._process
        if process is None or process.stdout is None:
            raise RuntimeError("relay daemon is not running")

        ready, _, _ = select.select([process.stdout], [], [], timeout)
        if not ready:
            raise TimeoutError("relay daemon response timed out")

        line = process.stdout.readline()
        if not line:
            raise RuntimeError("relay daemon exited unexpectedly")
        return line.strip()

    def _stop_unlocked(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return

        if process.poll() is None:
            try:
                if process.stdin is not None:
                    process.stdin.write("QUIT\n")
                    process.stdin.flush()
                process.wait(timeout=1.0)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                process.terminate()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()

    def _start_unlocked(self) -> None:
        if not DAEMON_CLASS.exists() or DAEMON_CLASS.stat().st_mtime < DAEMON_SOURCE.stat().st_mtime:
            compile_result = subprocess.run(
                [
                    "javac",
                    "--release",
                    "8",
                    "-cp",
                    str(DENKOVI_LIB_PATH),
                    str(DAEMON_SOURCE),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if compile_result.returncode != 0:
                raise RuntimeError(f"relay daemon compile failed: {compile_result.stderr.strip()}")

        classpath = os.pathsep.join((str(DAEMON_CLASS_DIR), str(DENKOVI_LIB_PATH)))
        self._process = subprocess.Popen(
            ["java", "-cp", classpath, "RelayDaemon", RELAY_SERIAL],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        response = self._readline(DAEMON_START_TIMEOUT)
        if response != "READY":
            raise RuntimeError(f"unexpected startup response: {response}")

    def start(self) -> bool:
        """Start and prewarm the JVM/USB connection without changing a relay."""
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return True

            self._stop_unlocked()
            try:
                self._start_unlocked()
                return True
            except Exception as exc:
                print(f"[RELAY] Persistent controller unavailable: {exc}")
                self._stop_unlocked()
                return False

    def pulse(self, relay_num: int, duration: float) -> bool:
        """Turn a relay on now and schedule it off inside the persistent JVM."""
        duration_ms = max(1, int(round(duration * 1000)))

        with self._lock:
            for attempt in range(2):
                if self._process is None or self._process.poll() is not None:
                    self._stop_unlocked()
                    try:
                        self._start_unlocked()
                    except Exception as exc:
                        print(f"[RELAY] Failed to start persistent controller: {exc}")
                        self._stop_unlocked()
                        return False

                try:
                    assert self._process is not None and self._process.stdin is not None
                    self._process.stdin.write(f"PULSE {relay_num} {duration_ms}\n")
                    self._process.stdin.flush()
                    response = self._readline(DAEMON_COMMAND_TIMEOUT)
                    if response.startswith("OK "):
                        elapsed_ms = response.split(" ", 1)[1]
                        print(f"[RELAY] Relay {relay_num} activated in {elapsed_ms} ms")
                        return True
                    print(f"[RELAY] Persistent controller error: {response}")
                except Exception as exc:
                    print(f"[RELAY] Persistent controller command failed: {exc}")

                self._stop_unlocked()
                if attempt == 0:
                    print("[RELAY] Reconnecting persistent controller")

            return False

    def stop(self) -> None:
        with self._lock:
            self._stop_unlocked()


_relay_daemon = RelayDaemonClient()
atexit.register(_relay_daemon.stop)


def warm_up_relay() -> bool:
    """Prewarm relay control at gateway startup; does not activate a relay."""
    ready = _relay_daemon.start()
    if ready:
        print("[RELAY] Persistent controller ready")
    return ready


def control_relay(relay_num: int, state: int) -> bool:
    """Compatibility fallback using the original one-shot Denkovi CLI."""
    command = [
        "java",
        "-jar",
        str(JAR_PATH),
        RELAY_SERIAL,
        "4v2",
        str(relay_num),
        str(state),
    ]

    try:
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode == 0:
            return True
        print(f"[RELAY] CLI error: {result.stderr.strip()}")
    except Exception as exc:
        print(f"[RELAY] Failed to run CLI fallback: {exc}")
    return False


def _legacy_pulse(relay_num: int, duration: float) -> bool:
    """Preserve access if the persistent controller is unavailable."""
    if not control_relay(relay_num, 1):
        return False
    time.sleep(duration)
    return control_relay(relay_num, 0)


def open_door(name: str, duration: float = 0.5) -> bool:
    """Immediately activate the named relay and turn it off after duration."""
    name_lower = name.lower()
    if name_lower not in RELAY_MAP:
        print(f"Unknown person: {name}")
        print(f"Valid names: {', '.join(RELAY_MAP.keys())}")
        return False

    relay_num = RELAY_MAP[name_lower]
    person_name = name.capitalize()
    print(f"Opening door for {person_name} (Relay {relay_num})...")

    if _relay_daemon.pulse(relay_num, duration):
        print(f"Door pulse scheduled for {person_name}")
        return True

    print("[RELAY] Falling back to one-shot controller")
    if _legacy_pulse(relay_num, duration):
        print(f"Door closed for {person_name}")
        return True

    print(f"Failed to open door for {person_name}")
    return False


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python relay_controller.py <name>")
        print(f"Valid names: {', '.join(RELAY_MAP.keys())}")
        sys.exit(1)
    open_door(sys.argv[1])


if __name__ == "__main__":
    main()
