# ResiClient - Raspberry Pi Access Control Gateway

RFID-based access control system that syncs with Firebase/Firestore and controls relay-operated doors.

## Features

- Real-time Firestore sync for resident data
- RFID tag validation against local SQLite cache
- Remote relay control via Firestore commands
- Pairing mode (10-second relay hold for RFID pairing)
- Automatic reconnection with watchdog monitoring
- Support for multiple addresses/relays

## Setup

### 1. Install Dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Firebase

Copy the service account template and fill in your credentials:

```bash
cp serviceAccountKey.json.template serviceAccountKey.json
# Edit serviceAccountKey.json with your Firebase credentials
```

### 3. Configure the Gateway

Edit `config.py` with your settings:

```python
COMMUNITY_NAME = 'YourCommunity'
COMMUNITY_STREET_NAME = 'Main Street'
COMMUNITY_STREET_NAME_HARVEY = 'Second Street'  # Optional second address
ENABLE_REMOTE_CONTROL = True
```

### 4. Run as a Service

Create systemd service at `/etc/systemd/system/resilive-gateway.service`:

```ini
[Unit]
Description=ResiLIVE Gateway Access Control
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=admin
WorkingDirectory=/home/admin/ResiClient
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/admin/ResiClient/.venv/bin/python gateway_runner.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl enable resilive-gateway.service
sudo systemctl start resilive-gateway.service
```

## Remote Commands

The gateway listens for commands in the Firestore `commands` collection:

- `open_gate` - Opens relay for 0.5 seconds
- `pairing_mode` - Opens relay for 10 seconds

## Hardware

- Raspberry Pi (tested on Pi 4/5)
- Denkovi USB relay board (4-channel)
- RFID reader (serial/USB)
