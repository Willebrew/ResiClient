#!/usr/bin/env python3
"""
gateway_runner.py — ResiLIVE Gateway Access Control

Interfaces with Firestore and a local SQLite database to manage community
access via RFID tags. Reads tags from a serial port, validates against
the community's allowed users, triggers relays, and logs access events.

Key features:
- Syncs Firestore community documents to local SQLite for fast access validation
- Listens for RFID tag reads over serial connection and validates them
- Logs access attempts (granted/denied) to ResiLIVE
- Supports remote gate-opening commands via Firestore commands collection
- Handles real-time Firestore updates via snapshot listeners
- Automatically reconnects to Firestore after network outages
- Offline log queue ensures no access events are lost during outages
- Tag debounce prevents duplicate reads from flooding the system
"""

import json
import os
import re
import sqlite3
import subprocess
import time
import threading
from pathlib import Path
from typing import List, Optional, Tuple

import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter
import serial
import requests

from relay_controller import open_door
from tag_6c import classify_tag_output, inspect_6c_candidate, match_6ctoc
from config import (
    COMMUNITY_NAME,
    COMMUNITY_STREET_NAME,
    COMMUNITY_STREET_NAME_HARVEY,
    LOG_URL,
    API_KEY,
    ENABLE_REMOTE_CONTROL,
)

# ── Paths ────────────────────────────────────────────────────────────────────
SERVICE_ACCOUNT_PATH = "serviceAccountKey.json"
SQLITE_PATH          = Path(__file__).with_name("communities.db")

# ── Serial config ────────────────────────────────────────────────────────────
SERIAL_PORT    = "/dev/ttyUSB0"
BAUDRATE       = 9600
SERIAL_TIMEOUT = 0.1

# ── Tag validation ───────────────────────────────────────────────────────────
TAG_LEN     = 13
TAG_PATTERN = re.compile(r"^[A-Z0-9.]+$")  # Valid tag chars: alphanumeric + dots
TAG_MIN_LEN = 8                              # Minimum length for a real tag

# ── Debounce / passback ─────────────────────────────────────────────────────
PASSBACK_TIMEOUT      = 10   # Seconds before the same tag can trigger again
PASSBACK_CLEANUP_INTERVAL = 60  # Seconds between old-entry cleanup passes

# ── Firestore watchdog ───────────────────────────────────────────────────────
WATCHDOG_INTERVAL     = 60    # Check connection every 60 seconds
WATCHDOG_TIMEOUT      = 900   # Consider stale if no snapshot in 15 minutes
RECONNECT_BACKOFF_MAX = 60    # Max backoff between reconnection attempts
WATCHDOG_MAX_FAILURES = 10    # Force process restart after this many consecutive failures

# ── Offline log queue ────────────────────────────────────────────────────────
LOG_FLUSH_INTERVAL = 30   # Seconds between flush attempts
LOG_FLUSH_BATCH    = 20   # Max pending logs to send per flush
LOG_MAX_ATTEMPTS   = 100  # Discard after this many failed attempts

# ── Heartbeat / diagnostics ──────────────────────────────────────────────────
HEARTBEAT_INTERVAL = 30   # Seconds between heartbeat POSTs
_start_time = time.time()

# Shared state for last tag read (updated by read_loop, read by heartbeat)
_last_tag_info = {"tag": None, "time": None, "status": None}
_last_tag_lock = threading.Lock()

# Serial port connection state
_serial_connected = False

# ── Firebase init ────────────────────────────────────────────────────────────
cred = credentials.Certificate(SERVICE_ACCOUNT_PATH)
firebase_admin.initialize_app(cred)
db = firestore.client()
communities_ref = db.collection("communities")
commands_ref    = db.collection("commands")

# ── SQLite init ──────────────────────────────────────────────────────────────
writer_conn = sqlite3.connect(SQLITE_PATH, check_same_thread=False, isolation_level=None)
writer_conn.execute("PRAGMA journal_mode=WAL")
writer_cur = writer_conn.cursor()

writer_cur.execute("""
    CREATE TABLE IF NOT EXISTS communities (
        id   TEXT PRIMARY KEY,
        data TEXT NOT NULL
    )
""")
writer_cur.execute("""
    CREATE TABLE IF NOT EXISTS pending_logs (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        payload    TEXT NOT NULL,
        created_at REAL NOT NULL,
        attempts   INTEGER DEFAULT 0
    )
""")
writer_conn.commit()

# ── Firestore connection state ───────────────────────────────────────────────
_last_snapshot_time = time.time()
_snapshot_lock      = threading.Lock()
_current_watch      = None
_watch_lock         = threading.Lock()


# ═══════════════════════════════════════════════════════════════════════════════
#  Snapshot time helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _update_snapshot_time() -> None:
    global _last_snapshot_time
    with _snapshot_lock:
        _last_snapshot_time = time.time()


def _get_snapshot_age() -> float:
    with _snapshot_lock:
        return time.time() - _last_snapshot_time


# ═══════════════════════════════════════════════════════════════════════════════
#  Tag lookup (unified — replaces duplicated functions)
# ═══════════════════════════════════════════════════════════════════════════════

def _tag_matches(scanned: str, stored) -> bool:
    """
    Compare a scanned tag to a stored playerId/id value.

    - 6C printed form ("NTTA 0000480314") in storage → match via match_6ctoc()
      against the scanned EPC hex.
    - ATA tags (no space) → exact match, with the old 12-character comparison
      retained as a compatibility fallback.
    """
    stored = str(stored or "").strip().upper()
    if not stored:
        return False
    if " " in stored:
        return match_6ctoc(stored, scanned)
    return scanned == stored or scanned[:TAG_LEN - 1] == stored[:TAG_LEN - 1]


def lookup_tag(tag: str, street_name: str) -> Tuple[bool, Optional[str]]:
    """
    Validate a tag and return its owner in a single DB pass.

    `tag` may be either an ATA short ID (12 chars, e.g. "DFW.06956066")
    or a 6C EPC hex string (24 or 28 chars, e.g. "31B03E000000030450...").

    Returns:
        (is_valid, owner_name)  — owner_name is None if the tag is a plain
        string entry or if lookup fails.
    """
    tag = tag.strip().upper()

    try:
        with sqlite3.connect(SQLITE_PATH, isolation_level=None) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            for (data_blob,) in conn.execute("SELECT data FROM communities"):
                try:
                    doc = json.loads(data_blob)
                except json.JSONDecodeError:
                    continue
                if COMMUNITY_NAME and doc.get("name") != COMMUNITY_NAME:
                    continue

                # 1) Top-level allowedUsers
                for u in doc.get("allowedUsers", []):
                    if isinstance(u, dict):
                        if _tag_matches(tag, u.get("id")) or _tag_matches(tag, u.get("playerId")):
                            return True, u.get("username")
                    else:
                        if _tag_matches(tag, u):
                            return True, None

                # 2) Nested addresses -> people
                for addr in doc.get("addresses", []):
                    if addr.get("street") != street_name:
                        continue
                    for p in addr.get("people", []):
                        if _tag_matches(tag, p.get("id")) or _tag_matches(tag, p.get("playerId")):
                            return True, p.get("username")
    except sqlite3.Error as e:
        print(f"[DB-ERROR] {e}")

    return False, None


# ═══════════════════════════════════════════════════════════════════════════════
#  Access logging with offline queue
# ═══════════════════════════════════════════════════════════════════════════════

def log_access(action: str, address: str = "", player: str = "Cloud") -> None:
    """Send an access-log event to ResiLIVE, queuing on failure."""
    payload = {
        "community": COMMUNITY_NAME,
        "player":    player,
        "action":    action,
    }
    if address:
        payload["address"] = address

    if not _send_log(payload):
        _enqueue_log(payload)


def _send_log(payload: dict) -> bool:
    """Attempt to POST a log payload. Returns True on success."""
    url = f"{LOG_URL.rstrip('/')}/log-access"
    try:
        res = requests.post(
            url,
            json=payload,
            headers={
                "X-API-Key":    API_KEY,
                "Content-Type": "application/json",
            },
            timeout=3,
        )
        if res.status_code >= 400:
            print(f"[LOG-WARN] HTTP {res.status_code} – {res.text}")
            return False
        return True
    except requests.RequestException as e:
        print(f"[LOG-WARN] {e}")
        return False


def _enqueue_log(payload: dict) -> None:
    """Store a failed log payload in SQLite for later retry."""
    try:
        writer_cur.execute(
            "INSERT INTO pending_logs (payload, created_at) VALUES (?, ?)",
            (json.dumps(payload), time.time()),
        )
        writer_conn.commit()
    except sqlite3.Error as e:
        print(f"[QUEUE-ERROR] Failed to enqueue log: {e}")


def _flush_pending_logs() -> None:
    """Background thread: periodically retries pending log entries."""
    while True:
        time.sleep(LOG_FLUSH_INTERVAL)
        try:
            rows = list(writer_cur.execute(
                "SELECT id, payload, attempts FROM pending_logs ORDER BY id ASC LIMIT ?",
                (LOG_FLUSH_BATCH,),
            ))
        except sqlite3.Error:
            continue

        for row_id, payload_str, attempts in rows:
            try:
                payload = json.loads(payload_str)
            except json.JSONDecodeError:
                _delete_pending_log(row_id)
                continue

            if _send_log(payload):
                _delete_pending_log(row_id)
            else:
                new_attempts = attempts + 1
                if new_attempts >= LOG_MAX_ATTEMPTS:
                    print(f"[QUEUE] Discarding log {row_id} after {new_attempts} attempts")
                    _delete_pending_log(row_id)
                else:
                    try:
                        writer_cur.execute(
                            "UPDATE pending_logs SET attempts = ? WHERE id = ?",
                            (new_attempts, row_id),
                        )
                        writer_conn.commit()
                    except sqlite3.Error:
                        pass


def _delete_pending_log(row_id: int) -> None:
    try:
        writer_cur.execute("DELETE FROM pending_logs WHERE id = ?", (row_id,))
        writer_conn.commit()
    except sqlite3.Error:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
#  Heartbeat / diagnostics sender
# ═══════════════════════════════════════════════════════════════════════════════

def _get_system_stats() -> dict:
    """Collect system stats for the heartbeat payload."""
    stats = {
        "community": COMMUNITY_NAME,
        "hostname": "",
        "ip": "",
    }

    # Uptime
    try:
        raw_uptime = subprocess.check_output(["uptime", "-p"], text=True).strip()
        stats["uptime"] = raw_uptime.replace("up ", "")
    except Exception:
        stats["uptime"] = "unknown"

    # Hostname
    try:
        stats["hostname"] = subprocess.check_output(["hostname"], text=True).strip()
    except Exception:
        pass

    # IP address (wlan0)
    try:
        out = subprocess.check_output(
            ["ip", "-4", "-o", "addr", "show", "wlan0"], text=True
        ).strip()
        # Parse "2: wlan0  inet 192.168.1.140/24 ..."
        parts = out.split()
        for i, p in enumerate(parts):
            if p == "inet" and i + 1 < len(parts):
                stats["ip"] = parts[i + 1].split("/")[0]
                break
    except Exception:
        pass

    # CPU usage (1-second sample)
    try:
        with open("/proc/stat") as f:
            line1 = f.readline()
        time.sleep(0.2)
        with open("/proc/stat") as f:
            line2 = f.readline()
        v1 = list(map(int, line1.split()[1:]))
        v2 = list(map(int, line2.split()[1:]))
        idle1, idle2 = v1[3], v2[3]
        total1, total2 = sum(v1), sum(v2)
        delta_total = total2 - total1
        delta_idle = idle2 - idle1
        if delta_total > 0:
            stats["cpu"] = round((1 - delta_idle / delta_total) * 100, 1)
    except Exception:
        pass

    # Memory
    try:
        with open("/proc/meminfo") as f:
            meminfo = {}
            for line in f:
                parts = line.split()
                meminfo[parts[0].rstrip(":")] = int(parts[1])
        total_kb = meminfo.get("MemTotal", 0)
        avail_kb = meminfo.get("MemAvailable", meminfo.get("MemFree", 0))
        stats["memTotalMb"] = round(total_kb / 1024)
        stats["memUsedMb"] = round((total_kb - avail_kb) / 1024)
    except Exception:
        pass

    # Disk
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - free
        stats["diskTotalGb"] = round(total / (1024 ** 3), 1)
        stats["diskUsedGb"] = round(used / (1024 ** 3), 1)
        stats["diskPct"] = round((used / total) * 100) if total else 0
    except Exception:
        pass

    # WiFi signal
    try:
        out = subprocess.check_output(["iwconfig", "wlan0"], text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            if "Signal level" in line:
                # "Signal level=-55 dBm"
                idx = line.index("Signal level=") + len("Signal level=")
                val = line[idx:].split()[0].replace("dBm", "")
                stats["wifiSignalDbm"] = int(val)
            if "ESSID:" in line:
                idx = line.index('ESSID:"') + len('ESSID:"')
                stats["wifiSsid"] = line[idx:].rstrip().rstrip('"')
    except Exception:
        pass

    # Firestore connection
    stats["firestoreConnected"] = _get_snapshot_age() < WATCHDOG_TIMEOUT

    # Serial port
    stats["serialConnected"] = _serial_connected

    # Pending logs count
    try:
        count = writer_cur.execute("SELECT count(*) FROM pending_logs").fetchone()[0]
        stats["pendingLogs"] = count
    except Exception:
        stats["pendingLogs"] = 0

    # Last tag info
    with _last_tag_lock:
        stats["lastTag"] = _last_tag_info["tag"]
        stats["lastTagTime"] = _last_tag_info["time"]
        stats["lastTagStatus"] = _last_tag_info["status"]

    return stats


def _heartbeat_loop() -> None:
    """Background thread: sends system stats to the ResiLIVE server every HEARTBEAT_INTERVAL."""
    url = f"{LOG_URL.rstrip('/')}/gateway/heartbeat"
    while True:
        try:
            payload = _get_system_stats()
            res = requests.post(
                url,
                json=payload,
                headers={
                    "X-API-Key": API_KEY,
                    "Content-Type": "application/json",
                },
                timeout=5,
            )
            if res.status_code >= 400:
                print(f"[HEARTBEAT-WARN] HTTP {res.status_code} – {res.text[:120]}")
        except Exception as e:
            print(f"[HEARTBEAT-WARN] {e}")
        time.sleep(HEARTBEAT_INTERVAL)


# ═══════════════════════════════════════════════════════════════════════════════
#  Firestore ↔ SQLite sync
# ═══════════════════════════════════════════════════════════════════════════════

def upsert(doc_id: str, payload: dict) -> None:
    writer_cur.execute(
        "REPLACE INTO communities (id, data) VALUES (?, ?)",
        (doc_id, json.dumps(payload, default=str)),
    )


def delete(doc_id: str) -> None:
    writer_cur.execute("DELETE FROM communities WHERE id = ?", (doc_id,))


def _wal_checkpoint() -> None:
    """Truncate the WAL file to prevent unbounded growth."""
    try:
        writer_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error as e:
        print(f"[DB-WARN] WAL checkpoint failed: {e}")


def initial_sync() -> None:
    """One-time full sync from Firestore to local SQLite."""
    print("🔄  Initial Firestore → SQLite sync…")
    fs_ids: List[str] = []

    for doc in communities_ref.stream():
        fs_ids.append(doc.id)
        upsert(doc.id, doc.to_dict())
        print(f"   ↳ synced {doc.id}")

    local_ids = [row[0] for row in writer_cur.execute("SELECT id FROM communities")]
    for stale in set(local_ids) - set(fs_ids):
        delete(stale)
        print(f"   ↳ removed stale {stale}")

    writer_conn.commit()
    _wal_checkpoint()
    print(f"✅  Sync complete ({len(fs_ids)} docs).")
    _update_snapshot_time()


def _resync_firestore() -> bool:
    """Re-sync after a reconnection. Returns True on success."""
    try:
        print("🔄  Re-syncing Firestore → SQLite…")
        fs_ids: List[str] = []

        for doc in communities_ref.stream():
            fs_ids.append(doc.id)
            upsert(doc.id, doc.to_dict())

        local_ids = [row[0] for row in writer_cur.execute("SELECT id FROM communities")]
        for stale in set(local_ids) - set(fs_ids):
            delete(stale)

        writer_conn.commit()
        _wal_checkpoint()
        print(f"✅  Re-sync complete ({len(fs_ids)} docs).")
        _update_snapshot_time()
        return True
    except Exception as e:
        print(f"[RESYNC-ERROR] {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
#  Firestore snapshot listener
# ═══════════════════════════════════════════════════════════════════════════════

def on_snapshot(_, changes, __) -> None:
    """Real-time Firestore listener callback."""
    _update_snapshot_time()

    for ch in changes:
        if ch.type.name == "ADDED":
            upsert(ch.document.id, ch.document.to_dict())
            print(f"🟢  ADDED    → {ch.document.id}")
        elif ch.type.name == "MODIFIED":
            upsert(ch.document.id, ch.document.to_dict())
            print(f"🟡  MODIFIED → {ch.document.id}")
        elif ch.type.name == "REMOVED":
            delete(ch.document.id)
            print(f"🔴  REMOVED  → {ch.document.id}")
    writer_conn.commit()


def _check_firestore_connection() -> bool:
    """Lightweight Firestore connectivity check."""
    try:
        next(communities_ref.limit(1).stream(), None)
        return True
    except Exception as e:
        print(f"[WATCHDOG] Connection check failed: {e}")
        return False


def _setup_snapshot_listener():
    """(Re-)establish the Firestore communities snapshot listener."""
    global _current_watch
    with _watch_lock:
        if _current_watch is not None:
            try:
                _current_watch.unsubscribe()
            except Exception:
                pass

        _current_watch = communities_ref.on_snapshot(on_snapshot)
        _update_snapshot_time()
        return _current_watch


def _firestore_watchdog() -> None:
    """
    Background thread monitoring Firestore connectivity.

    Key fix: when the connection check PASSES and there were no prior failures,
    we simply reset the snapshot timer — the listener is fine, there just
    haven't been any real changes. We only re-sync + re-establish when
    recovering from an actual outage.
    """
    consecutive_failures = 0
    backoff = 5

    print("🔍  Firestore watchdog started")

    while True:
        time.sleep(WATCHDOG_INTERVAL)

        snapshot_age = _get_snapshot_age()

        if snapshot_age <= WATCHDOG_TIMEOUT:
            # Connection is healthy
            if consecutive_failures > 0:
                print("✅  [WATCHDOG] Connection stable")
            consecutive_failures = 0
            continue

        # Snapshot is stale — check if Firestore is actually reachable
        print(f"⚠️  [WATCHDOG] No Firestore updates in {int(snapshot_age)}s, checking connection…")

        if not _check_firestore_connection():
            # Firestore is actually unreachable
            consecutive_failures += 1
            print(f"❌  [WATCHDOG] Firestore unreachable (attempt {consecutive_failures})")

            # Force restart if stuck too long - systemd will respawn us
            if consecutive_failures >= WATCHDOG_MAX_FAILURES:
                print(f"💀  [WATCHDOG] {consecutive_failures} consecutive failures - forcing restart to reset gRPC channel")
                os._exit(1)

            wait_time = min(backoff * (2 ** (consecutive_failures - 1)), RECONNECT_BACKOFF_MAX)
            print(f"⏳  [WATCHDOG] Waiting {wait_time}s before retry…")
            time.sleep(wait_time)
            continue

        # Firestore IS reachable
        if consecutive_failures == 0:
            # Connection was never lost — listener is fine, just quiet
            _update_snapshot_time()
            continue

        # Recovering from a real outage — re-sync and re-establish listeners
        print("🔄  [WATCHDOG] Connection restored, re-establishing listener…")

        if _resync_firestore():
            _setup_snapshot_listener()
            _setup_commands_listener()
            consecutive_failures = 0
            print("✅  [WATCHDOG] Firestore listener re-established")
        else:
            consecutive_failures += 1
            print("❌  [WATCHDOG] Re-sync failed, will retry")


# ═══════════════════════════════════════════════════════════════════════════════
#  Remote commands listener
# ═══════════════════════════════════════════════════════════════════════════════

_commands_watch      = None
_commands_watch_lock = threading.Lock()


def _on_command_snapshot(doc_snapshot, changes, read_time) -> None:
    """Firestore listener callback for remote commands."""
    for change in changes:
        if change.type.name != "ADDED":
            continue

        doc = change.document
        cmd = doc.to_dict()

        if cmd.get("community") != COMMUNITY_NAME:
            continue

        command_type = cmd.get("command")
        if command_type not in ["open_gate", "pairing_mode"]:
            try:
                doc.reference.delete()
            except Exception:
                pass
            continue

        addr_req = (cmd.get("address") or "").strip()

        if addr_req:
            if addr_req not in [COMMUNITY_STREET_NAME, COMMUNITY_STREET_NAME_HARVEY]:
                try:
                    doc.reference.delete()
                except Exception:
                    pass
                continue
            target_address = addr_req
        else:
            target_address = COMMUNITY_STREET_NAME

        if command_type == "pairing_mode":
            print(f"[PAIRING] Pairing mode request for {target_address or 'default'} - opening for 10 seconds")
            perform_grant_access(skip_log=False, address=target_address, duration=10.0)
        else:
            print(f"[REMOTE] Remote-controller request for {target_address or 'default'} - opening gate")
            perform_grant_access(skip_log=True, address=target_address)

        try:
            doc.reference.delete()
            print(f"[COMMAND] Processed and deleted command: {command_type}")
        except Exception as e:
            print(f"[COMMAND-ERROR] Failed to delete command: {e}")


def _setup_commands_listener():
    """(Re-)establish the Firestore commands listener."""
    global _commands_watch
    with _commands_watch_lock:
        if _commands_watch is not None:
            try:
                _commands_watch.unsubscribe()
            except Exception:
                pass

        _commands_watch = commands_ref.where(
            filter=FieldFilter("community", "==", COMMUNITY_NAME)
        ).on_snapshot(_on_command_snapshot)
        return _commands_watch


# ═══════════════════════════════════════════════════════════════════════════════
#  Relay / access granting
# ═══════════════════════════════════════════════════════════════════════════════

def perform_grant_access(skip_log: bool = False, address: str = None, duration: float = None) -> None:
    """Trigger the appropriate relay based on address."""
    if address == COMMUNITY_STREET_NAME_HARVEY:
        relay_name = "harvey"
        street = COMMUNITY_STREET_NAME_HARVEY
    else:
        relay_name = "jones"
        street = COMMUNITY_STREET_NAME or ""

    try:
        if duration:
            open_door(relay_name, duration=duration)
        else:
            open_door(relay_name)
        print(f"🚪  Gate opened for {relay_name.capitalize()}")
    except Exception as e:
        print(f"[RELAY-ERROR] Failed to open {relay_name} door: {e}")

    if not skip_log:
        log_access("Access granted (Remote)", street)


# ═══════════════════════════════════════════════════════════════════════════════
#  Main RFID read loop
# ═══════════════════════════════════════════════════════════════════════════════

def read_loop() -> None:
    """
    Main loop: reads RFID tags, validates, triggers relays, and logs access.

    Includes debounce logic to prevent duplicate tag reads from flooding
    the system, and filters out reader firmware strings.
    """
    global _serial_connected
    print(f"📡  Listening on {SERIAL_PORT} @ {BAUDRATE} bps")
    try:
        ser = serial.Serial(SERIAL_PORT, BAUDRATE, timeout=SERIAL_TIMEOUT)
        _serial_connected = True
    except serial.SerialException as e:
        print(f"[SERIAL-ERROR] {e}")
        _serial_connected = False
        return

    # Passback / debounce state
    last_seen: dict[str, float] = {}
    last_cleanup = time.time()

    try:
        while True:
            raw_bytes = ser.readline()
            raw = raw_bytes.decode("ascii", errors="ignore").strip()

            if not raw or not raw.startswith("#"):
                continue

            # Preserve exact framed bytes in journald for reader diagnostics.
            print(f"[SERIAL-RAW] hex={raw_bytes.hex().upper()} text={raw!r}")

            # ATA tags arrive as "#DFW.05913102 4C...^?$"; 6C tags as
            # "#31B03E000000030450153F3EF493" with just CRLF. Split on "..."
            # strips the ATA trailer; for 6C there is no "..." so the body
            # is the full hex EPC including the PC word.
            payload = raw[1:].split("...", 1)[0]
            payload_parts = payload.split()
            body = payload_parts[0].upper() if payload_parts else ""
            # Filter junk/garbled reads but keep short legitimate tags like
            # the "JACK" hangtag that emit "#JACK\r\n" (4 chars).
            if not body or not TAG_PATTERN.match(body):
                continue

            if len(body) > 15:
                print(f"[6C-DETAIL] {json.dumps(inspect_6c_candidate(body), sort_keys=True)}")

            classified = classify_tag_output(body)
            if classified is None:
                print(f"[TAG-DROP] Unrecognized or unknown-agency output: {body!r}")
                continue

            tag_key = classified.lookup_value
            display = classified.display_value

            # ── Debounce: skip if same tag seen within PASSBACK_TIMEOUT ──
            now = time.time()
            if tag_key in last_seen and (now - last_seen[tag_key]) < PASSBACK_TIMEOUT:
                continue
            last_seen[tag_key] = now

            # Periodic cleanup of stale debounce entries
            if now - last_cleanup > PASSBACK_CLEANUP_INTERVAL:
                cutoff = now - PASSBACK_TIMEOUT * 2
                last_seen = {k: v for k, v in last_seen.items() if v > cutoff}
                last_cleanup = now

            # ── Validate and process ─────────────────────────────────────
            print(f"READ '{display}' – checking… ", end="", flush=True)

            # Check Harvey first
            valid, owner = lookup_tag(tag_key, COMMUNITY_STREET_NAME_HARVEY)
            if valid:
                owner = owner or "Unknown"
                print("Accepted (Harvey)")
                with _last_tag_lock:
                    _last_tag_info.update(tag=display, time=int(time.time() * 1000), status="granted")
                try:
                    open_door("harvey")
                except Exception as e:
                    print(f"[RELAY-ERROR] Failed to open Harvey door: {e}")
                log_access(
                    f"Access granted via tag: {display}",
                    COMMUNITY_STREET_NAME_HARVEY or "",
                    owner,
                )
                continue

            # Check Jones
            valid, owner = lookup_tag(tag_key, COMMUNITY_STREET_NAME)
            if valid:
                owner = owner or "Unknown"
                print("Accepted (Jones)")
                with _last_tag_lock:
                    _last_tag_info.update(tag=display, time=int(time.time() * 1000), status="granted")
                try:
                    open_door("jones")
                except Exception as e:
                    print(f"[RELAY-ERROR] Failed to open Jones door: {e}")
                log_access(
                    f"Access granted via tag: {display}",
                    COMMUNITY_STREET_NAME or "",
                    owner,
                )
                continue

            # Denied
            print("Denied")
            with _last_tag_lock:
                _last_tag_info.update(tag=display, time=int(time.time() * 1000), status="denied")
            log_access(
                f"Access denied, invalid tag: {display}",
                COMMUNITY_STREET_NAME or "",
                "Unknown",
            )

    except KeyboardInterrupt:
        print("👋  Shutting down")
    finally:
        ser.close()


# ═══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # 1. Initial Firestore → SQLite sync
    initial_sync()

    # 2. Real-time Firestore listener (communities)
    _setup_snapshot_listener()

    # 3. Firestore watchdog for auto-reconnect
    threading.Thread(target=_firestore_watchdog, daemon=True).start()

    # 4. Offline log queue flush thread
    threading.Thread(target=_flush_pending_logs, daemon=True).start()

    # 5. Heartbeat / diagnostics sender
    threading.Thread(target=_heartbeat_loop, daemon=True).start()
    print("[HEARTBEAT] Diagnostics heartbeat started")

    # 6. Remote commands listener
    if ENABLE_REMOTE_CONTROL:
        _setup_commands_listener()
        print("[REMOTE] Firestore command listener started")
    else:
        print("[REMOTE] Remote control disabled")

    try:
        # 6. Main RFID read loop (blocks until Ctrl+C)
        read_loop()
    finally:
        with _watch_lock:
            if _current_watch is not None:
                _current_watch.unsubscribe()
        with _commands_watch_lock:
            if _commands_watch is not None:
                _commands_watch.unsubscribe()
        writer_conn.close()
        print("Goodbye!")
