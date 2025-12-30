#!/usr/bin/env python3
import subprocess
import time
import sys
import os

# Path to the JAR file
JAR_PATH = '/home/admin/pi-transcore-access-control/DenkoviRelayCommandLineTool.jar'

# Name to relay mapping
RELAY_MAP = {
    'jones': 1,
    'harvey': 2
}

def control_relay(relay_num, state):
    """Control relay using JAR file
    relay_num: 1-4
    state: 1 for ON, 0 for OFF
    """
    cmd = ['java', '-jar', JAR_PATH, '0007252401', '4v2', str(relay_num), str(state)]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return True
        else:
            print(f"Error: {result.stderr}")
            return False
    except Exception as e:
        print(f"Failed to run command: {e}")
        return False

def open_door(name, duration=0.5):
    """Open door for a person by name
    name: 'jones' or 'harvey'
    duration: how long to keep relay on (seconds)
    """
    name_lower = name.lower()
    
    if name_lower not in RELAY_MAP:
        print(f"Unknown person: {name}")
        print(f"Valid names: {', '.join(RELAY_MAP.keys())}")
        return False
    
    relay_num = RELAY_MAP[name_lower]
    person_name = name.capitalize()
    
    print(f"Opening door for {person_name} (Relay {relay_num})...")
    
    # Turn relay ON
    if control_relay(relay_num, 1):
        time.sleep(duration)
        # Turn relay OFF
        control_relay(relay_num, 0)
        print(f"Door closed for {person_name}")
        return True
    else:
        print(f"Failed to open door for {person_name}")
        return False

def main():
    """Main function to handle command line arguments"""
    if len(sys.argv) < 2:
        print("Usage: python relay_control.py <name>")
        print(f"Valid names: {', '.join(RELAY_MAP.keys())}")
        sys.exit(1)
    
    name = sys.argv[1]
    open_door(name)

if __name__ == "__main__":
    main()
