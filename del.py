#!/usr/bin/env python3
"""
print_communities_db.py

Utility script to dump the contents of the local 'communities' SQLite database.
Each row’s JSON payload will be parsed and pretty-printed.
"""

import json
import sqlite3
import sys
from pathlib import Path

# Path to the DB file—by default assumes this script lives next to 'communities.db'
DB_PATH = Path(__file__).with_name("communities.db")

def fetch_all_communities(db_path: Path):
    """
    Connects to the SQLite DB at db_path, fetches all rows from 'communities',
    and returns a list of tuples (doc_id, data_dict).
    """
    if not db_path.exists():
        print(f"Error: database file not found at {db_path}", file=sys.stderr)
        sys.exit(1)
    
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        cur = conn.cursor()
        cur.execute("SELECT id, data FROM communities ORDER BY id")
        rows = cur.fetchall()
        conn.close()
        results = []
        for doc_id, data_blob in rows:
            try:
                data = json.loads(data_blob)
            except json.JSONDecodeError:
                data = {"_raw": data_blob}
            results.append((doc_id, data))
        return results

    except sqlite3.Error as e:
        print(f"SQLite error: {e}", file=sys.stderr)
        sys.exit(1)

def pretty_print_communities(comms):
    """
    Given a list of (doc_id, data_dict), print them nicely.
    """
    if not comms:
        print("No communities found in the database.")
        return

    for doc_id, data in comms:
        print("=" * 80)
        print(f"Document ID: {doc_id}")
        print(json.dumps(data, indent=2, sort_keys=True))
    print("=" * 80)
    print(f"Total documents: {len(comms)}")

def main():
    comms = fetch_all_communities(DB_PATH)
    pretty_print_communities(comms)

if __name__ == "__main__":
    main()

