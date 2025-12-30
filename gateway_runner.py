#!/usr/bin/env python3
"""
gateway_runnerwlogging.py

This script acts as an access control gateway to ResiLIVE, interfacing with Firestore and a local SQLite database
to manage community access via RFID tags. It listens to a serial port for RFID reads, verifies tags
against the community's allowed users, triggers relays, and logs access events to ResiLIVE.

Key features:
- Syncs Firestore community documents to local SQLite for fast access validation
- Listens for RFID tag reads over serial connection and validates them
- Logs access attempts (granted/denied) to ResiLIVE
- Supports remote gate-opening commands via polling an HTTP endpoint
- Handles real-time Firestore updates via snapshot listeners
- Automatically reconnects to Firestore after network outages

The system operates in two main modes:
1. Local RFID tag validation - reading tags from a serial port reader
2. Remote command polling - allowing authorized remote gate opening

This gateway is designed for resilient operation with local caching to ensure
functionality even during temporary network outages.
"""
# Standard library imports for data handling, database, timing and concurrency
import json, sqlite3, time, threading
from pathlib import Path
from typing import List, Optional

# Firebase Admin SDK for Firestore database access
import firebase_admin
from firebase_admin import credentials, firestore
# PySerial for RFID reader communication
import serial
# Requests for HTTP communication with logging service and remote controller
import requests

from relay_controller import open_door

# Import configuration values from config.py
from config import (
    COMMUNITY_NAME,        # Name of the community this gateway serves
    COMMUNITY_STREET_NAME, # Street address of the community location
    COMMUNITY_STREET_NAME_HARVEY, # Street address of the community location
    LOG_URL,              # URL endpoint for access logging
    API_KEY,              # API key for authentication with remote services
    ENABLE_REMOTE_CONTROL, # Flag to enable/disable remote control via Vercel endpoint
)

# Path to Firebase service account credentials file
SERVICE_ACCOUNT_PATH = "serviceAccountKey.json"
# Path to SQLite database file (stored in same directory as this script)
SQLITE_PATH          = Path(__file__).with_name("communities.db")

# Serial port configuration for RFID reader
SERIAL_PORT    = "/dev/ttyUSB0"  # USB port where RFID reader is connected
BAUDRATE       = 9600           # Communication speed with the reader
SERIAL_TIMEOUT = 0.1           # Timeout in seconds for serial read operations

# Length of RFID tag strings to process
TAG_LEN = 13

# Firestore watchdog configuration
WATCHDOG_INTERVAL = 60        # Check connection every 60 seconds
WATCHDOG_TIMEOUT = 300        # Consider connection dead if no update in 5 minutes
RECONNECT_BACKOFF_MAX = 60    # Maximum backoff time between reconnection attempts

# Initialize Firebase Admin SDK with service account credentials
cred = credentials.Certificate(SERVICE_ACCOUNT_PATH)
firebase_admin.initialize_app(cred)
# Get Firestore client and reference to collections
db = firestore.client()
communities_ref = db.collection("communities")
commands_ref = db.collection("commands")  # For remote commands from UI

# Initialize SQLite connection for writing community data
# check_same_thread=False allows access from multiple threads
# isolation_level=None enables autocommit mode
writer_conn = sqlite3.connect(SQLITE_PATH, check_same_thread=False, isolation_level=None)
# Enable Write-Ahead Logging for better concurrency and crash recovery
writer_conn.execute("PRAGMA journal_mode=WAL")
writer_cur = writer_conn.cursor()
# Create communities table if it doesn't exist
writer_cur.execute(
    """
    CREATE TABLE IF NOT EXISTS communities (
        id   TEXT PRIMARY KEY,  -- Firestore document ID
        data TEXT NOT NULL     -- JSON serialized document data
    )
"""
)
writer_conn.commit()  # Ensure table creation is committed

# Firestore connection state tracking
_last_snapshot_time = time.time()  # Track when we last received a snapshot
_snapshot_lock = threading.Lock()   # Thread-safe access to snapshot state
_current_watch = None               # Current Firestore listener reference
_watch_lock = threading.Lock()      # Thread-safe access to watch reference


def _update_snapshot_time() -> None:
    """Update the last snapshot time to indicate the connection is alive."""
    global _last_snapshot_time
    with _snapshot_lock:
        _last_snapshot_time = time.time()


def _get_snapshot_age() -> float:
    """Get seconds since last snapshot update."""
    with _snapshot_lock:
        return time.time() - _last_snapshot_time


def get_tag_owner(tag: str) -> Optional[str]:
    """
    Return the *username* associated with `tag`, or None if unknown.

    The lookup mirrors the search paths used by `is_tag_valid` so we don't disturb
    any existing validation logic.
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

                # 1) top-level allowedUsers (could be strings or dicts)
                for u in doc.get("allowedUsers", []):
                    if isinstance(u, dict):
                        if tag == str(u.get("id", "")).upper()[: TAG_LEN - 1] \
                        or tag == str(u.get("playerId", "")).upper()[: TAG_LEN - 1]:
                            return u.get("username")
                    else:  # string entry, no username stored
                        if tag == str(u).upper()[: TAG_LEN - 1]:
                            return None

                # 2) nested addresses -> people
                for addr in doc.get("addresses", []):
                    if addr.get("street") != COMMUNITY_STREET_NAME:
                        continue
                    for p in addr.get("people", []):
                        if tag in {
                            str(p.get("id", "")).upper()[: TAG_LEN - 1],
                            str(p.get("playerId", "")).upper()[: TAG_LEN - 1],
                        }:
                            return p.get("username")
    except sqlite3.Error as e:
        print(f"[DB-ERROR] {e}")

    return None

def get_tag_owner_harvey(tag: str) -> Optional[str]:
    """
    Return the *username* associated with `tag`, or None if unknown.

    The lookup mirrors the search paths used by `is_tag_valid` so we don't disturb
    any existing validation logic.
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

                # 1) top-level allowedUsers (could be strings or dicts)
                for u in doc.get("allowedUsers", []):
                    if isinstance(u, dict):
                        if tag == str(u.get("id", "")).upper()[: TAG_LEN - 1] \
                        or tag == str(u.get("playerId", "")).upper()[: TAG_LEN - 1]:
                            return u.get("username")
                    else:  # string entry, no username stored
                        if tag == str(u).upper()[: TAG_LEN - 1]:
                            return None

                # 2) nested addresses -> people
                for addr in doc.get("addresses", []):
                    if addr.get("street") != COMMUNITY_STREET_NAME_HARVEY:
                        continue
                    for p in addr.get("people", []):
                        if tag in {
                            str(p.get("id", "")).upper()[: TAG_LEN - 1],
                            str(p.get("playerId", "")).upper()[: TAG_LEN - 1],
                        }:
                            return p.get("username")
    except sqlite3.Error as e:
        print(f"[DB-ERROR] {e}")

    return None

def log_access(action: str,
               address: str = "",
               player: str = "Cloud") -> None:
    """
    Send an access-log event to ResiLIVE.

    `player` defaults to "Cloud" so every existing call that doesn't
    care about the owner keeps behaving exactly as before.
    """
    url = f"{LOG_URL.rstrip('/')}/log-access"
    payload = {
        "community": COMMUNITY_NAME,
        "player":    player,
        "action":    action,
    }
    if address:
        payload["address"] = address

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
            print(f"[LOG-ERROR] HTTP {res.status_code} – {res.text}")
    except requests.RequestException as e:
        print(f"[LOG-ERROR] {e}")

def upsert(doc_id: str, payload: dict) -> None:
    """
    Insert or update a community document in the local SQLite database.

    Args:
        doc_id (str): The Firestore document ID to insert or update.
        payload (dict): The document data to store (will be JSON serialized).
    """
    writer_cur.execute(
        "REPLACE INTO communities (id, data) VALUES (?, ?)",
        (doc_id, json.dumps(payload, default=str))  # Convert Python dict to JSON string
    )

def delete(doc_id: str) -> None:
    """
    Remove a community document from the local SQLite database.

    Args:
        doc_id (str): The Firestore document ID to delete from local cache.
    """
    writer_cur.execute("DELETE FROM communities WHERE id = ?", (doc_id,))

def initial_sync() -> None:
    """
    Perform a one-time sync from Firestore to the local SQLite database.

    This function:
    1. Fetches all community documents from Firestore
    2. Stores them in the local SQLite database
    3. Removes any stale records that no longer exist in Firestore

    This ensures the local database is an accurate mirror of Firestore
    at startup, enabling fast local access validation even if the
    network connection is lost later.
    """
    print("🔄  Initial Firestore → SQLite sync…")
    fs_ids: List[str] = []  # Track Firestore document IDs

    # Fetch all documents from Firestore and store locally
    for doc in communities_ref.stream():
        fs_ids.append(doc.id)
        upsert(doc.id, doc.to_dict())
        print(f"   ↳ synced {doc.id}")

    # Find and remove any documents in SQLite that no longer exist in Firestore
    local_ids = [row[0] for row in writer_cur.execute("SELECT id FROM communities")]
    for stale in set(local_ids) - set(fs_ids):
        delete(stale)
        print(f"   ↳ removed stale {stale}")

    writer_conn.commit()  # Ensure all changes are committed
    print(f"✅  Sync complete ({len(fs_ids)} docs).")
    _update_snapshot_time()  # Mark connection as alive


def _resync_firestore() -> bool:
    """
    Re-sync data from Firestore after a reconnection.

    Returns:
        bool: True if sync was successful, False otherwise.
    """
    try:
        print("🔄  Re-syncing Firestore → SQLite…")
        fs_ids: List[str] = []

        for doc in communities_ref.stream():
            fs_ids.append(doc.id)
            upsert(doc.id, doc.to_dict())

        # Remove stale records
        local_ids = [row[0] for row in writer_cur.execute("SELECT id FROM communities")]
        for stale in set(local_ids) - set(fs_ids):
            delete(stale)

        writer_conn.commit()
        print(f"✅  Re-sync complete ({len(fs_ids)} docs).")
        _update_snapshot_time()
        return True
    except Exception as e:
        print(f"[RESYNC-ERROR] {e}")
        return False


def on_snapshot(_, changes, __) -> None:
    """
    Firestore snapshot listener callback for real-time updates.

    This function is called automatically whenever changes occur in the
    Firestore 'communities' collection. It processes document changes
    (additions, modifications, deletions) and synchronizes them to the
    local SQLite database.

    Args:
        _ : Unused Firestore collection snapshot parameter.
        changes: List of document change events from Firestore.
        __ : Unused read time parameter.
    """
    # Update timestamp to indicate connection is alive
    _update_snapshot_time()

    for ch in changes:
        if ch.type.name == "ADDED":
            # New document added to Firestore - add to local DB
            upsert(ch.document.id, ch.document.to_dict())
            print(f"🟢  ADDED    → {ch.document.id}")
        elif ch.type.name == "MODIFIED":
            # Existing document modified in Firestore - update local DB
            upsert(ch.document.id, ch.document.to_dict())
            print(f"🟡  MODIFIED → {ch.document.id}")
        elif ch.type.name == "REMOVED":
            # Document deleted from Firestore - remove from local DB
            delete(ch.document.id)
            print(f"🔴  REMOVED  → {ch.document.id}")
    writer_conn.commit()  # Commit all changes to ensure data persistence


def _check_firestore_connection() -> bool:
    """
    Check if Firestore connection is alive by attempting a simple query.

    Returns:
        bool: True if connection is working, False otherwise.
    """
    try:
        # Try to fetch just the first document to verify connectivity
        next(communities_ref.limit(1).stream(), None)
        return True
    except Exception as e:
        print(f"[WATCHDOG] Connection check failed: {e}")
        return False


def _setup_snapshot_listener():
    """
    Set up the Firestore snapshot listener.

    Returns:
        The watch object for the listener.
    """
    global _current_watch
    with _watch_lock:
        # Unsubscribe from existing listener if any
        if _current_watch is not None:
            try:
                _current_watch.unsubscribe()
            except Exception:
                pass  # Ignore errors when unsubscribing broken listener

        # Create new listener
        _current_watch = communities_ref.on_snapshot(on_snapshot)
        _update_snapshot_time()
        return _current_watch


def _firestore_watchdog() -> None:
    """
    Background thread that monitors Firestore connection and reconnects if needed.

    This watchdog:
    1. Periodically checks if we've received recent snapshot updates
    2. Attempts to verify Firestore connectivity
    3. Re-establishes the listener and re-syncs data if connection is broken
    4. Uses exponential backoff for reconnection attempts
    """
    backoff = 5  # Initial backoff time in seconds
    consecutive_failures = 0

    print("🔍  Firestore watchdog started")

    while True:
        time.sleep(WATCHDOG_INTERVAL)

        snapshot_age = _get_snapshot_age()

        # If we haven't received updates in a while, check connection
        if snapshot_age > WATCHDOG_TIMEOUT:
            print(f"⚠️  [WATCHDOG] No Firestore updates in {int(snapshot_age)}s, checking connection…")

            if not _check_firestore_connection():
                consecutive_failures += 1
                print(f"❌  [WATCHDOG] Firestore unreachable (attempt {consecutive_failures})")

                # Calculate backoff with exponential increase
                wait_time = min(backoff * (2 ** (consecutive_failures - 1)), RECONNECT_BACKOFF_MAX)
                print(f"⏳  [WATCHDOG] Waiting {wait_time}s before retry…")
                time.sleep(wait_time)
                continue

            # Connection is back - re-sync and re-establish listener
            print("🔄  [WATCHDOG] Connection restored, re-establishing listener…")

            if _resync_firestore():
                _setup_snapshot_listener()
                _setup_commands_listener()
                consecutive_failures = 0
                print("✅  [WATCHDOG] Firestore listener re-established")
            else:
                consecutive_failures += 1
                print("❌  [WATCHDOG] Re-sync failed, will retry")
        else:
            # Connection appears healthy
            if consecutive_failures > 0:
                print("✅  [WATCHDOG] Connection stable")
            consecutive_failures = 0


def is_tag_valid(tag: str) -> bool:
    """
    Check if a given RFID tag is valid for access to the community.

    This function queries the local SQLite database to validate the tag against:
    1. The community's allowedUsers list
    2. People associated with the community's street address

    Args:
        tag (str): The RFID tag string to validate.

    Returns:
        bool: True if the tag is valid for access, False otherwise.
    """
    # Normalize tag format (strip whitespace and convert to uppercase)
    tag = tag.strip().upper()

    try:
        # Create a new read-only connection to avoid thread conflicts
        with sqlite3.connect(SQLITE_PATH, isolation_level=None) as conn:
            conn.execute("PRAGMA journal_mode=WAL")  # Use WAL mode for better concurrency

            # Iterate through all community documents
            for (data_blob,) in conn.execute("SELECT data FROM communities"):
                try:
                    doc = json.loads(data_blob)  # Parse JSON data
                except json.JSONDecodeError:
                    continue  # Skip invalid JSON

                # Skip documents for other communities if COMMUNITY_NAME is set
                if COMMUNITY_NAME and doc.get("name") != COMMUNITY_NAME:
                    continue

                # Check 1: Is tag in the community's allowedUsers list?
                for u in doc.get("allowedUsers", []):
                    if isinstance(u, dict):
                        if tag == str(u.get("id", "")).upper()[: TAG_LEN - 1] or \
                           tag == str(u.get("playerId", "")).upper()[: TAG_LEN - 1]:
                            return True
                    else:
                        if tag == str(u).upper()[: TAG_LEN - 1]:
                            return True

                # Check 2: Is tag associated with a person at the community's street address?
                for addr in doc.get("addresses", []):
                    if addr.get("street") != COMMUNITY_STREET_NAME:
                        continue  # Skip addresses that don't match our street

                    for p in addr.get("people", []):
                        # Check both id and playerId fields (truncated to TAG_LEN-1)
                        if tag in {
                            str(p.get("id", "")).upper()[: TAG_LEN - 1],
                            str(p.get("playerId", "")).upper()[: TAG_LEN - 1],
                        }:
                            return True
        return False  # Tag not found in any valid entry
    except sqlite3.Error as e:
        print(f"[DB-ERROR] {e}")
        return False  # Return false on database errors

def is_tag_valid_Jones(tag: str) -> bool:
    """
    Check if a given RFID tag is valid for access to the community.

    This function queries the local SQLite database to validate the tag against:
    1. The community's allowedUsers list
    2. People associated with the community's street address

    Args:
        tag (str): The RFID tag string to validate.

    Returns:
        bool: True if the tag is valid for access, False otherwise.
    """
    # Normalize tag format (strip whitespace and convert to uppercase)
    tag = tag.strip().upper()

    try:
        # Create a new read-only connection to avoid thread conflicts
        with sqlite3.connect(SQLITE_PATH, isolation_level=None) as conn:
            conn.execute("PRAGMA journal_mode=WAL")  # Use WAL mode for better concurrency

            # Iterate through all community documents
            for (data_blob,) in conn.execute("SELECT data FROM communities"):
                try:
                    doc = json.loads(data_blob)  # Parse JSON data
                except json.JSONDecodeError:
                    continue  # Skip invalid JSON

                # Skip documents for other communities if COMMUNITY_NAME is set
                if COMMUNITY_NAME and doc.get("name") != COMMUNITY_NAME:
                    continue

                # Check 1: Is tag in the community's allowedUsers list?
                for u in doc.get("allowedUsers", []):
                    if isinstance(u, dict):
                        if tag == str(u.get("id", "")).upper()[: TAG_LEN - 1] or \
                           tag == str(u.get("playerId", "")).upper()[: TAG_LEN - 1]:
                            return True
                    else:
                        if tag == str(u).upper()[: TAG_LEN - 1]:
                            return True

                # Check 2: Is tag associated with a person at the community's street address?
                for addr in doc.get("addresses", []):
                    if addr.get("street") != COMMUNITY_STREET_NAME:
                        continue  # Skip addresses that don't match our street

                    for p in addr.get("people", []):
                        # Check both id and playerId fields (truncated to TAG_LEN-1)
                        if tag in {
                            str(p.get("id", "")).upper()[: TAG_LEN - 1],
                            str(p.get("playerId", "")).upper()[: TAG_LEN - 1],
                        }:
                            return True
        return False  # Tag not found in any valid entry
    except sqlite3.Error as e:
        print(f"[DB-ERROR] {e}")
        return False  # Return false on database errors

def is_tag_valid_Harvey(tag: str) -> bool:
    """
    Check if a given RFID tag is valid for access to the community.

    This function queries the local SQLite database to validate the tag against:
    1. The community's allowedUsers list
    2. People associated with the community's street address

    Args:
        tag (str): The RFID tag string to validate.

    Returns:
        bool: True if the tag is valid for access, False otherwise.
    """
    # Normalize tag format (strip whitespace and convert to uppercase)
    tag = tag.strip().upper()

    try:
        # Create a new read-only connection to avoid thread conflicts
        with sqlite3.connect(SQLITE_PATH, isolation_level=None) as conn:
            conn.execute("PRAGMA journal_mode=WAL")  # Use WAL mode for better concurrency

            # Iterate through all community documents
            for (data_blob,) in conn.execute("SELECT data FROM communities"):
                try:
                    doc = json.loads(data_blob)  # Parse JSON data
                except json.JSONDecodeError:
                    continue  # Skip invalid JSON

                # Skip documents for other communities if COMMUNITY_NAME is set
                if COMMUNITY_NAME and doc.get("name") != COMMUNITY_NAME:
                    continue

                # Check 1: Is tag in the community's allowedUsers list?
                for u in doc.get("allowedUsers", []):
                    if isinstance(u, dict):
                        if tag == str(u.get("id", "")).upper()[: TAG_LEN - 1] or \
                           tag == str(u.get("playerId", "")).upper()[: TAG_LEN - 1]:
                            return True
                    else:
                        if tag == str(u).upper()[: TAG_LEN - 1]:
                            return True

                # Check 2: Is tag associated with a person at the community's street address?
                for addr in doc.get("addresses", []):
                    if addr.get("street") != COMMUNITY_STREET_NAME_HARVEY:
                        continue  # Skip addresses that don't match our street

                    for p in addr.get("people", []):
                        # Check both id and playerId fields (truncated to TAG_LEN-1)
                        if tag in {
                            str(p.get("id", "")).upper()[: TAG_LEN - 1],
                            str(p.get("playerId", "")).upper()[: TAG_LEN - 1],
                        }:
                            return True
        return False  # Tag not found in any valid entry
    except sqlite3.Error as e:
        print(f"[DB-ERROR] {e}")
        return False  # Return false on database errors

def read_loop() -> None:
    """
    Main loop: continuously reads RFID tags from the serial port and processes them.

    This function:
    1. Opens a serial connection to the RFID reader
    2. Continuously reads tag data from the serial port
    3. Validates each tag against the local database
    4. Triggers the appropriate relay for access control
    5. Logs access attempts (granted/denied) to the remote logging service
    6. Runs until interrupted by a KeyboardInterrupt (Ctrl+C)
    """
    # Attempt to open the serial port for the RFID reader
    print(f"📡  Listening on {SERIAL_PORT} @ {BAUDRATE} bps")
    try:
        ser = serial.Serial(SERIAL_PORT, BAUDRATE, timeout=SERIAL_TIMEOUT)
    except serial.SerialException as e:
        print(f"[SERIAL-ERROR] {e}")
        return  # Exit if we can't open the serial port

    try:
        # Main processing loop
        while True:
            # Read a line from the serial port and clean it up
            raw = ser.readline().decode("ascii", errors="ignore").strip()

            # Skip empty lines or lines that don't start with the expected marker
            if not raw or not raw.startswith("#"):
                continue

            # Extract the tag portion and convert to uppercase
            tag_key = raw[1:TAG_LEN].upper()
            print(f"READ '{tag_key}' – checking… ", end="", flush=True)

            # Validate the tag and process accordingly
            if is_tag_valid_Harvey(tag_key):
                # Access granted for Harvey - get owner name if available
                owner = get_tag_owner_harvey(tag_key) or "Unknown"
                print("Accepted (Harvey)")

                # Open the Harvey relay
                try:
                    open_door("harvey")
                except Exception as e:
                    print(f"[RELAY-ERROR] Failed to open Harvey door: {e}")

                # Log the successful access attempt
                log_access(
                    f"Access granted via tag: {tag_key}",
                    COMMUNITY_STREET_NAME_HARVEY or "",
                    owner
                )

            elif is_tag_valid_Jones(tag_key):
                # Access granted for Jones - get owner name if available
                owner = get_tag_owner(tag_key) or "Unknown"
                print("Accepted (Jones)")

                # Open the Jones relay
                try:
                    open_door("jones")
                except Exception as e:
                    print(f"[RELAY-ERROR] Failed to open Jones door: {e}")

                # Log the successful access attempt
                log_access(
                    f"Access granted via tag: {tag_key}",
                    COMMUNITY_STREET_NAME or "",
                    owner
                )
            else:
                # Access denied - tag not recognized
                print("Denied")

                # Log the denied access attempt
                log_access(
                    f"Access denied, invalid tag: {tag_key}",
                    COMMUNITY_STREET_NAME or "",
                    "Unknown"
                )
    except KeyboardInterrupt:
        # Handle clean shutdown on Ctrl+C
        print("👋  Shutting down")
    finally:
        # Ensure serial port is closed even if an exception occurs
        ser.close()

def perform_grant_access(skip_log: bool = False, address: str = None, duration: float = None) -> None:
    """
    Trigger the appropriate relay to grant access based on the address.

    This function:
    1. Determines which relay to trigger based on the address
    2. Opens the appropriate door using the relay controller
    3. Logs the access event to the logging service if skip_log is False

    Args:
        skip_log (bool): If True, skips logging the access event. Defaults to False.
        address (str): The address to determine which relay to trigger. If None, defaults to Jones.
        duration (float): How long to keep the relay open in seconds. If None, uses default (0.5s).
    """
    # Determine which relay to trigger
    if address == COMMUNITY_STREET_NAME_HARVEY:
        relay_name = "harvey"
        street = COMMUNITY_STREET_NAME_HARVEY
    else:
        relay_name = "jones"
        street = COMMUNITY_STREET_NAME or ""

    try:
        # Open the appropriate door (with optional custom duration)
        if duration:
            open_door(relay_name, duration=duration)
        else:
            open_door(relay_name)
        print(f"🚪  Gate opened for {relay_name.capitalize()}")
    except Exception as e:
        print(f"[RELAY-ERROR] Failed to open {relay_name} door: {e}")

    # Log the access event if not skipped
    if not skip_log:
        log_access("Access granted (Remote)", street)


# Track the commands listener
_commands_watch = None
_commands_watch_lock = threading.Lock()


def _on_command_snapshot(doc_snapshot, changes, read_time) -> None:
    """
    Firestore snapshot listener callback for remote commands.

    This function is called when commands are added to the 'commands' collection.
    It processes commands intended for this community and deletes them after execution.
    """
    for change in changes:
        if change.type.name != "ADDED":
            continue  # Only process new commands

        doc = change.document
        cmd = doc.to_dict()

        # Check if command is for this community
        if cmd.get("community") != COMMUNITY_NAME:
            continue

        command_type = cmd.get("command")
        if command_type not in ["open_gate", "pairing_mode"]:
            # Unknown command - delete it and continue
            try:
                doc.reference.delete()
            except Exception:
                pass
            continue

        # Get the requested address from the command
        addr_req = (cmd.get("address") or "").strip()

        # Validate address if specified
        if addr_req:
            if addr_req not in [COMMUNITY_STREET_NAME, COMMUNITY_STREET_NAME_HARVEY]:
                # Invalid address - delete command and skip
                try:
                    doc.reference.delete()
                except Exception:
                    pass
                continue
            target_address = addr_req
        else:
            target_address = COMMUNITY_STREET_NAME

        # Process the command
        if command_type == "pairing_mode":
            print(f"[PAIRING] Pairing mode request for {target_address or 'default'} - opening for 10 seconds")
            perform_grant_access(skip_log=False, address=target_address, duration=10.0)
        else:
            print(f"[REMOTE] Remote-controller request for {target_address or 'default'} - opening gate")
            perform_grant_access(skip_log=True, address=target_address)

        # Delete the command after processing
        try:
            doc.reference.delete()
            print(f"[COMMAND] Processed and deleted command: {command_type}")
        except Exception as e:
            print(f"[COMMAND-ERROR] Failed to delete command: {e}")


def _setup_commands_listener():
    """
    Set up the Firestore listener for remote commands.

    Returns:
        The watch object for the commands listener.
    """
    global _commands_watch
    with _commands_watch_lock:
        # Unsubscribe from existing listener if any
        if _commands_watch is not None:
            try:
                _commands_watch.unsubscribe()
            except Exception:
                pass

        # Create listener for commands targeting this community
        _commands_watch = commands_ref.where("community", "==", COMMUNITY_NAME).on_snapshot(_on_command_snapshot)
        return _commands_watch

if __name__ == "__main__":
    # Entry point of the script when run directly

    # Step 1: Perform initial sync from Firestore to local SQLite database
    initial_sync()

    # Step 2: Set up real-time listener for Firestore changes (communities)
    _setup_snapshot_listener()

    # Step 3: Start the Firestore watchdog thread for automatic reconnection
    threading.Thread(target=_firestore_watchdog, daemon=True).start()

    # Step 4: Set up Firestore listener for remote commands if enabled
    if ENABLE_REMOTE_CONTROL:
        _setup_commands_listener()
        print("[REMOTE] Firestore command listener started")
    else:
        print("[REMOTE] Remote control disabled")

    try:
        # Step 5: Enter the main RFID reading loop (blocks until interrupted)
        read_loop()
    finally:
        # Step 6: Clean up resources when exiting
        with _watch_lock:
            if _current_watch is not None:
                _current_watch.unsubscribe()
        with _commands_watch_lock:
            if _commands_watch is not None:
                _commands_watch.unsubscribe()
        writer_conn.close()
        print("Goodbye!")
