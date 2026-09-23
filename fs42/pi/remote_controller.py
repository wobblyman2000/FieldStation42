#!/usr/bin/env python3

from evdev import InputDevice, ecodes, list_devices
import requests
import os
import subprocess
import threading
import sys
import time
import json
import argparse
import select


# ======================================
# CONFIGURATION - CUSTOMIZE YOUR MAPPINGS
# ======================================

# Server Configuration - can be overridden by environment variables
FS42_HOST = os.getenv('FS42_HOST', '127.0.0.1')
FS42_PORT = os.getenv('FS42_PORT', '4242')
FS42_BASE_URL = f"http://{FS42_HOST}:{FS42_PORT}"

# Debounce Configuration - Time in seconds to wait between repeated button presses
DEBOUNCE_TIME = 0.25  # 250ms default debounce time

# Services to toggle when power button is pressed (remote-controller is never toggled)
SYSTEMCTL_TO_TOGGLE = [
    'fs42.service',           # Field Player
    'fs42-cable-box.service', # Cable Box
    'fs42-osd.service',       # On-Screen Display
]

USE_SYSTEMCTL = True

# Key Mappings - Change these to customize which keys do what
# Available key names: 'home', 'end', 'up', 'down', 'left', 'right', 'space', 'enter',
# 'esc', 'tab', 'backspace', 'delete', 'insert', 'pageup', 'pagedown', 'f1'-'f12',
# 'a'-'z', 'leftshift', 'rightshift', 'leftctrl', 'rightctrl', 'leftalt', 'rightalt'
DEFAULT_KEY_MAPPINGS = {
    # Remote control functions
    'show_guide': ['home', 'g', 'c', 'guide', 'menu', 'info'],        # Show program guide
    'volume_up': ['right', 'volumeup'],                            # Increase volume
    'volume_down': ['left', 'volumedown'],                          # Decrease volume
    'channel_up': ['up', 'pageup', 'channelup'],                    # Next channel
    'channel_down': ['down', 'pagedown', 'channeldown'],            # Previous channel
    'last_channel': ['backspace', 'delete', 'b'],                   # Switch to last channel
    'mute': ['m', 'mute'],                                          # Mute/unmute volume
    'toggle_subtitles': ['v'],                                      # Toggle subtitle visibility in mpv
    'cycle_subtitles': ['j'],                                       # Cycle subtitle tracks in mpv
    'cycle_audio': ['a'],                                           # Cycle audio tracks in mpv
    'power_stop': ['end', 'space', 'p', 'power'],                   # Stop player (power button)
    'exit': ['esc', 'q'],                                           # Exit remote controller
}
SETTINGS_FILE = "runtime/remote_controller.json"
KEY_MAPPINGS = DEFAULT_KEY_MAPPINGS.copy()

def is_key_matched(key_name, mapped_key):
    if isinstance(mapped_key, (list, tuple, set)):
        return key_name in mapped_key
    return key_name == mapped_key

if os.path.exists(SETTINGS_FILE):
    print(f"Loading settings from {SETTINGS_FILE}")
    try:
        with open(SETTINGS_FILE, 'r') as file:
            remote_settings = json.load(file)
        if remote_settings.get("use_systemctl") is not None:
            print("Loading USE_SYSTEMCTL from file")
            USE_SYSTEMCTL = bool(remote_settings["use_systemctl"])
        if remote_settings.get("systemctl_to_toggle") is not None:
            print("Loading SYSTEMCTL_TO_TOGGLE from file")
            SYSTEMCTL_TO_TOGGLE = remote_settings["systemctl_to_toggle"]
        if remote_settings.get("key_mappings") is not None:
            print("Loading KEY_MAPPINGS from file")
            file_mappings = remote_settings["key_mappings"]
            KEY_MAPPINGS = file_mappings.copy()
            for function, key in DEFAULT_KEY_MAPPINGS.items():
                if function not in KEY_MAPPINGS:
                    KEY_MAPPINGS[function] = key
    except Exception as e:
        print(f"Error loading settings from file: {e}")

CALLBACK_MAP_FILE = "runtime/remote_callback_map.json"
PRESS_SOCKET = "runtime/press.socket"

# Channel input state
channel_input = ''
channel_input_timer = None
input_lock = threading.Lock()

# Last channel tracking
current_channel = None
last_channel = None

# Debounce tracking - stores last press time for each function
last_press_time = {}
debounce_lock = threading.Lock()

def read_exec_mappings():
    mappings = {}
    if not os.path.exists(CALLBACK_MAP_FILE):
        return mappings
    with open(CALLBACK_MAP_FILE, "r") as f:
        contents = f.read()
        try:
            mappings = json.loads(contents)
        except Exception as e:
            print(e)
            pass
        return mappings

exec_mappings = read_exec_mappings()

def write_press_socket(digits):
    try:
        with open(PRESS_SOCKET, "w") as fp:
            json.dump({"digits": digits, "ts": time.time()}, fp)
    except Exception as e:
        print(f"Failed to write press socket: {e}")


def clear_press_socket():
    try:
        with open(PRESS_SOCKET, "w") as fp:
            fp.write("")
    except Exception:
        pass


def should_allow_press(function_name, debounce_time=DEBOUNCE_TIME):
    """Check if enough time has passed since last press of this function"""
    global last_press_time

    with debounce_lock:
        current_time = time.time()
        last_time = last_press_time.get(function_name, 0)

        if current_time - last_time >= debounce_time:
            last_press_time[function_name] = current_time
            return True
        else:
            return False


def send_channel_change():
    """Send accumulated channel input to the server"""
    global channel_input, channel_input_timer, current_channel, last_channel
    
    with input_lock:
        if channel_input:
            try:
                channel_number = int(channel_input)
                print(f"Sending channel change to {channel_number}")
                response = requests.get(f'{FS42_BASE_URL}/player/channels/{channel_number}')
                if response.ok:
                    print(f"Changed to channel {channel_number}")
                    # Update channel tracking - only update last_channel if we know the current channel
                    if current_channel is not None and current_channel != channel_number:
                        last_channel = current_channel
                        print(f"Updated last_channel: {last_channel} (was on {current_channel}, now on {channel_number})")
                    current_channel = channel_number
                else:
                    print(f"Channel change to {channel_number} failed")
            except Exception as e:
                print(f"Channel change error: {e}")
            
            # Clear the input
            channel_input = ''
            channel_input_timer = None
            clear_press_socket()


def send_parental_digit_if_active(number):
    try:
        status_response = requests.get(f'{FS42_BASE_URL}/player/status', timeout=2)
        if not status_response.ok:
            return False

        status = status_response.json()
        if not status.get("awaiting_pin"):
            return False

        response = requests.post(
            f'{FS42_BASE_URL}/player/parental/digit/{number}',
            timeout=2
        )

        if response.ok:
            print(f"Parental controls PIN digit entered: {number}")
            return True

        print(f"Parental controls digit failed: {response.status_code}")
        return False
    except Exception as e:
        print(f"Parental controls digit error: {e}")
        return False


def number_pressed(number):
    """Handle number key presses from remote"""
    global channel_input, channel_input_timer

    if send_parental_digit_if_active(number):
        return

    with input_lock:
        # Cancel any existing timer
        if channel_input_timer:
            channel_input_timer.cancel()
            channel_input_timer = None
        
        # Add digit to input
        channel_input += str(number)
        print(f"Channel input: {channel_input}")
        write_press_socket(channel_input)
        
        # If we have 3 digits, send immediately
        if len(channel_input) >= 3:
            # Use a very short timer to avoid race conditions
            channel_input_timer = threading.Timer(0.1, send_channel_change)
            channel_input_timer.start()
        else:
            # Set timeout for 1 second
            channel_input_timer = threading.Timer(1.0, send_channel_change)
            channel_input_timer.start()


def show_guide_pressed():
    """Handle home key press"""
    if not should_allow_press('show_guide'):
        return  # Debounced - ignore this press

    global current_channel, last_channel
    try:
        response = requests.post(f'{FS42_BASE_URL}/player/channels/guide')
        if response.ok:
            print("Guide displayed")
            if current_channel is not None:
                last_channel = current_channel
                print(f"Stored last_channel: {last_channel} before guide")
        else:
            print("Guide command failed")
    except Exception as e:
        print(f"Guide error: {e}")


def volume_up_pressed():
    """Handle right arrow key press"""
    if not should_allow_press('volume_up'):
        return  # Debounced - ignore this press

    try:
        response = requests.get(f'{FS42_BASE_URL}/player/volume/up')
        if response.ok:
            data = response.json()
            print(f"Volume up: {data.get('volume', 'success')}")
        else:
            print("Volume up failed")
    except Exception as e:
        print(f"Volume up error: {e}")


def volume_down_pressed():
    """Handle left arrow key press"""
    if not should_allow_press('volume_down'):
        return  # Debounced - ignore this press

    try:
        response = requests.get(f'{FS42_BASE_URL}/player/volume/down')
        if response.ok:
            data = response.json()
            print(f"Volume down: {data.get('volume', 'success')}")
        else:
            print("Volume down failed")
    except Exception as e:
        print(f"Volume down error: {e}")


def mute_pressed():
    """Handle mute key press"""
    if not should_allow_press('mute'):
        return  # Debounced - ignore this press

    try:
        response = requests.get(f'{FS42_BASE_URL}/player/volume/mute')
        if response.ok:
            data = response.json()
            print(f"Mute toggled: {data.get('volume', 'success')}")
        else:
            print("Mute toggle failed")
    except Exception as e:
        print(f"Mute error: {e}")


def channel_up_pressed():
    """Handle up arrow key press"""
    if not should_allow_press('channel_up'):
        return  # Debounced - ignore this press

    global current_channel, last_channel
    try:
        response = requests.get(f'{FS42_BASE_URL}/player/channels/up')
        if response.ok:
            print("Channel up success")
            # Poll status to wait for channel change (max 1 second)
            try:
                old_channel = current_channel
                new_channel = None
                max_attempts = 20  # 20 attempts * 0.05s = 1 second max

                for attempt in range(max_attempts):
                    time.sleep(0.05)
                    status_response = requests.get(f'{FS42_BASE_URL}/player/status')
                    if status_response.ok:
                        status = status_response.json()
                        new_channel = status.get('channel_number')

                        # If channel changed or we have a channel number, we're done
                        if new_channel != old_channel:
                            break

                print(f"DEBUG: Status returned channel_number={new_channel}, current_channel={current_channel}")
                # Only update last_channel if the channel actually changed
                if new_channel != current_channel and current_channel is not None:
                    last_channel = current_channel
                    print(f"Channel changed: {current_channel} -> {new_channel} (Last: {last_channel})")
                else:
                    print(f"Channel unchanged: new={new_channel}, current={current_channel}")
                current_channel = new_channel
            except Exception as e:
                print(f"Failed to get current channel: {e}")
                current_channel = None
        else:
            print("Channel up failed")
    except Exception as e:
        print(f"Channel up error: {e}")


def channel_down_pressed():
    """Handle down arrow key press"""
    if not should_allow_press('channel_down'):
        return  # Debounced - ignore this press

    global current_channel, last_channel
    try:
        response = requests.get(f'{FS42_BASE_URL}/player/channels/down')
        if response.ok:
            print("Channel down success")
            # Poll status to wait for channel change (max 1 second)
            try:
                old_channel = current_channel
                new_channel = None
                max_attempts = 20  # 20 attempts * 0.05s = 1 second max

                for attempt in range(max_attempts):
                    time.sleep(0.05)
                    status_response = requests.get(f'{FS42_BASE_URL}/player/status')
                    if status_response.ok:
                        status = status_response.json()
                        new_channel = status.get('channel_number')

                        # If channel changed or we have a channel number, we're done
                        if new_channel != old_channel:
                            break

                print(f"DEBUG: Status returned channel_number={new_channel}, current_channel={current_channel}")
                # Only update last_channel if the channel actually changed
                if new_channel != current_channel and current_channel is not None:
                    last_channel = current_channel
                    print(f"Channel changed: {current_channel} -> {new_channel} (Last: {last_channel})")
                else:
                    print(f"Channel unchanged: new={new_channel}, current={current_channel}")
                current_channel = new_channel
            except Exception as e:
                print(f"Failed to get current channel: {e}")
                current_channel = None
        else:
            print("Channel down failed")
    except Exception as e:
        print(f"Channel down error: {e}")


def last_channel_pressed():
    """Handle last channel key press"""
    if not should_allow_press('last_channel'):
        return  # Debounced - ignore this press

    global current_channel, last_channel
    if last_channel is not None:
        try:
            print(f"Switching to last channel: {last_channel}")
            response = requests.get(f'{FS42_BASE_URL}/player/channels/{last_channel}')
            if response.ok:
                print(f"Switched to last channel {last_channel}")
                # Swap current and last channel
                temp = current_channel
                current_channel = last_channel
                last_channel = temp
            else:
                print(f"Last channel change to {last_channel} failed")
        except Exception as e:
            print(f"Last channel error: {e}")
    else:
        print("No last channel stored")


def end_pressed():
    """Handle end key press - stop the player"""
    if not should_allow_press('power_stop'):
        return  # Debounced - ignore this press

    try:
        response = requests.get(f'{FS42_BASE_URL}/player/commands/stop')
        if response.ok:
            print("Player stopped")
        else:
            print("Stop command failed")
    except Exception as e:
        print(f"Stop error: {e}")


def toggle_services():
    """Toggle FieldStation42 services on/off - true power button behavior"""
    if not should_allow_press('power_stop'):
        return  # Debounced - ignore this press

    try:
        # Check if fs42.service is active to determine current state
        result = subprocess.run(
            ['systemctl', '--user', 'is-active', 'fs42.service'],
            capture_output=True,
            text=True,
            timeout=5
        )

        services_active = result.stdout.strip() == 'active'

        if services_active:
            # Services are running, stop them
            for service in SYSTEMCTL_TO_TOGGLE:
                try:
                    # Fire and forget - don't wait for completion since services may have delays
                    subprocess.Popen(
                        ['systemctl', '--user', 'stop', service],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL
                    )
                    print(f"Stopping {service}")
                except Exception as e:
                    print(f"Error stopping {service}: {e}")
        else:
            # Services are not running, start them
            for service in SYSTEMCTL_TO_TOGGLE:
                try:
                    # Fire and forget - don't wait for completion since services have startup delays
                    subprocess.Popen(
                        ['systemctl', '--user', 'start', service],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL
                    )
                    print(f"Starting {service}")
                except Exception as e:
                    print(f"Error starting {service}: {e}")

    except subprocess.TimeoutExpired:
        print("Error: systemctl command timed out")
    except Exception as e:
        print(f"Error toggling services: {e}")


def find_input_device(device_spec=None):
    """Find the Flirc or keyboard input device

    Args:
        device_spec: Optional device specifier. Can be:
            - Device path (e.g., '/dev/input/event3')
            - Device index (e.g., '0', '1', '2')
            - Device name pattern (e.g., 'flirc', 'keyboard')
            - None to auto-detect
    """
    import glob
    devices = []
    default = False
    # Look for input devices
    for device_path in glob.glob('/dev/input/event*'):
        try:
            device = InputDevice(device_path)
            # Look for devices that can send key events
            if ecodes.EV_KEY in device.capabilities():
                devices.append((device_path, device.name))
        except (OSError, PermissionError):
            continue

    if not devices:
        print("No keyboard/remote devices found. Run with sudo?")
        return None

    print("Available input devices:")
def find_input_devices(device_spec=None):
    """Find input device(s) based on specification or auto-detect all keyboards and remotes"""
    try:
        all_paths = list_devices()
        devices = [InputDevice(path) for path in all_paths]
    except Exception as e:
        print(f"Error accessing input devices: {e}")
        return []

    if not devices:
        print("No input devices found!")
        return []

    print("\nAvailable input devices:")
    for i, dev in enumerate(devices):
        print(f"{i}: {dev.name} ({dev.path})")

    # If specific path given
    if device_spec:
        if device_spec.startswith('/dev/input/'):
            if os.path.exists(device_spec):
                print(f"Using specified device path: {device_spec}")
                return [device_spec]
            else:
                print(f"Warning: Device path '{device_spec}' not found, falling back to auto-detect")

        elif device_spec.isdigit():
            index = int(device_spec)
            if 0 <= index < len(devices):
                dev = devices[index]
                print(f"Using device at index {index}: {dev.name} ({dev.path})")
                return [dev.path]
            else:
                print(f"Warning: Device index {index} out of range (0-{len(devices)-1}), falling back to auto-detect")

        else:
            spec_lower = device_spec.lower()
            matched = [dev.path for dev in devices if spec_lower in dev.name.lower()]
            if matched:
                print(f"Found requested device(s) matching '{device_spec}': {len(matched)} device(s)")
                return matched

    # Auto-detect mode: return ALL connected keyboard & remote control input devices
    matched_paths = []
    for dev in devices:
        name_lower = dev.name.lower()
        if any(ex in name_lower for ex in ['power button', 'sleep button', 'video bus']):
            continue

        caps = dev.capabilities()
        if ecodes.EV_KEY in caps:
            keys = caps[ecodes.EV_KEY]
            if ecodes.KEY_A in keys or ecodes.KEY_ENTER in keys or ecodes.KEY_1 in keys:
                print(f"Matched keyboard/remote device: {dev.name} ({dev.path})")
                matched_paths.append(dev.path)
            elif any(k in name_lower for k in ['keyboard', 'flirc', 'remote', 'control']):
                print(f"Matched named device: {dev.name} ({dev.path})")
                matched_paths.append(dev.path)

    if not matched_paths and devices:
        print(f"Fallback to first device: {devices[0].name} ({devices[0].path})")
        matched_paths = [devices[0].path]

    return matched_paths

def get_key_name_from_code(key_code):
    """Convert evdev key code to readable key name"""
    key_map = {
        ecodes.KEY_HOME: 'home', ecodes.KEY_END: 'end',
        ecodes.KEY_UP: 'up', ecodes.KEY_DOWN: 'down',
        ecodes.KEY_LEFT: 'left', ecodes.KEY_RIGHT: 'right',
        ecodes.KEY_SPACE: 'space', ecodes.KEY_ENTER: 'enter',
        ecodes.KEY_ESC: 'esc', ecodes.KEY_TAB: 'tab',
        ecodes.KEY_BACKSPACE: 'backspace', ecodes.KEY_DELETE: 'delete',
        ecodes.KEY_INSERT: 'insert', ecodes.KEY_PAGEUP: 'pageup',
        ecodes.KEY_PAGEDOWN: 'pagedown',
        ecodes.KEY_VOLUMEUP: 'volumeup',
        ecodes.KEY_VOLUMEDOWN: 'volumedown',
        ecodes.KEY_MUTE: 'mute',
        ecodes.KEY_CHANNELUP: 'channelup',
        ecodes.KEY_CHANNELDOWN: 'channeldown',
        ecodes.KEY_POWER: 'power',
        ecodes.KEY_BACK: 'backspace',
        ecodes.KEY_HOMEPAGE: 'home',
        ecodes.KEY_MENU: 'menu',
        ecodes.KEY_INFO: 'info',
        ecodes.KEY_F1: 'f1', ecodes.KEY_F2: 'f2', ecodes.KEY_F3: 'f3',
        ecodes.KEY_F4: 'f4', ecodes.KEY_F5: 'f5', ecodes.KEY_F6: 'f6',
        ecodes.KEY_F7: 'f7', ecodes.KEY_F8: 'f8', ecodes.KEY_F9: 'f9',
        ecodes.KEY_F10: 'f10', ecodes.KEY_F11: 'f11', ecodes.KEY_F12: 'f12',
        ecodes.KEY_LEFTSHIFT: 'leftshift', ecodes.KEY_RIGHTSHIFT: 'rightshift',
        ecodes.KEY_LEFTCTRL: 'leftctrl', ecodes.KEY_RIGHTCTRL: 'rightctrl',
        ecodes.KEY_LEFTALT: 'leftalt', ecodes.KEY_RIGHTALT: 'rightalt',
    }

    # Add letter keys a-z using ecodes constants
    for char_code in range(ord('a'), ord('z') + 1):
        attr = f"KEY_{chr(char_code).upper()}"
        if hasattr(ecodes, attr):
            key_map[getattr(ecodes, attr)] = chr(char_code)

    return key_map.get(key_code)


def handle_key_name(key_name):
    """Handle a key press by key name (common logic for evdev and test mode)"""
    # Check mappings and call appropriate function
    for function_name, mapped_key in KEY_MAPPINGS.items():
        if is_key_matched(key_name, mapped_key) or key_name == function_name:
            print(f"DEBUG: Matched function: {function_name}")
            if function_name == 'show_guide':
                show_guide_pressed()
            elif function_name == 'volume_up':
                volume_up_pressed()
            elif function_name == 'volume_down':
                volume_down_pressed()
            elif function_name == 'mute':
                mute_pressed()
            elif function_name == 'toggle_subtitles':
                mpv_command_pressed('toggle_subtitles')
            elif function_name == 'cycle_subtitles':
                mpv_command_pressed('cycle_subtitles')
            elif function_name == 'cycle_audio':
                mpv_command_pressed('cycle_audio')
            elif function_name == 'channel_up':
                channel_up_pressed()
            elif function_name == 'channel_down':
                channel_down_pressed()
            elif function_name == 'last_channel':
                last_channel_pressed()
            elif function_name == 'power_stop':
                if USE_SYSTEMCTL:
                    toggle_services()
                else:
                    end_pressed()
            elif function_name == 'exit':
                print("Exiting remote controller...")
                return False
            return True

    if key_name in exec_mappings:
        try:
            print("Executing key callback: ", exec_mappings[key_name])
            os.system(exec_mappings[key_name])
        except Exception as e:
            print("Error executing key callback")
            print(e)

    return True


def handle_key_event(event):
    """Handle key press events from evdev"""
    if event.type == ecodes.EV_KEY and event.value in (1, 2):  # Key press or repeat
        key_code = event.code

        # Number keys (1-9, 0) and Keypad keys (KP1-KP9, KP0)
        if key_code >= ecodes.KEY_1 and key_code <= ecodes.KEY_9:
            number = key_code - ecodes.KEY_1 + 1
            number_pressed(number)
            return True
        elif key_code == ecodes.KEY_0:
            number_pressed(0)
            return True
        elif key_code >= ecodes.KEY_KP1 and key_code <= ecodes.KEY_KP9:
            number = key_code - ecodes.KEY_KP1 + 1
            number_pressed(number)
            return True
        elif key_code == ecodes.KEY_KP0:
            number_pressed(0)
            return True
        elif key_code in (ecodes.KEY_ENTER, ecodes.KEY_KPENTER):
            global channel_input_timer
            with input_lock:
                confirming = bool(channel_input)
                if confirming and channel_input_timer:
                    channel_input_timer.cancel()
                    channel_input_timer = None
            if confirming:
                send_channel_change()
                return True

        # Get the key name and check if it's mapped to a function
        key_name = get_key_name_from_code(key_code)
        if not key_name:
            print(f"DEBUG: Unknown key code: {key_code}")
            return True

        print(f"DEBUG: Key pressed: {key_name} (code: {key_code})")
        return handle_key_name(key_name)

    return True


def test_mode():
    """Run in test mode using stdin for input (for WSL/development)"""
    print("\n" + "="*60)
    print("TEST MODE - Running without input device")
    print("="*60)
    print("\nAvailable commands:")
    print("  0-9: Channel selection")
    print("  h: Home/Guide")
    print("  u: Channel up")
    print("  d: Channel down")
    print("  l: Volume down (left)")
    print("  r: Volume up (right)")
    print("  m: Mute")
    print("  b: Last channel")
    print("  e: Power/End (toggle services)")
    print("  q: Quit")
    print("\nType a command and press Enter:")
    print("="*60 + "\n")

    while True:
        try:
            cmd = input("> ").strip().lower()

            if not cmd:
                continue

            # Handle numbers
            if cmd.isdigit():
                for digit in cmd:
                    number_pressed(int(digit))
                continue

            # Map single character commands to key names
            key_map = {
                'h': 'home',
                'u': 'up',
                'd': 'down',
                'l': 'left',
                'r': 'right',
                'm': 'm',
                'b': 'backspace',
                'e': 'end',
                'q': 'esc',
            }

            key_name = key_map.get(cmd)
            if key_name:
                if not handle_key_name(key_name):
                    break
            else:
                print(f"Unknown command: {cmd}")

        except KeyboardInterrupt:
            print("\nExiting remote controller...")
            break
        except EOFError:
            break


def main():
    """Main function to start the input device listener"""
    # Parse command-line arguments
    parser = argparse.ArgumentParser(
        description='FieldStation42 Remote Controller',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Device specification examples:
  -d /dev/input/event3      Use specific device path
  -d 0                      Use device at index 0 from the device list
  -d flirc                  Use first device with 'flirc' in its name
  -d keyboard               Use first device with 'keyboard' in its name
  -d test                   Run test mode

Environment variables:
  FS42_INPUT_DEVICE         Device specification (same format as -d)
  FS42_HOST                 Server host (default: 127.0.0.1)
  FS42_PORT                 Server port (default: 4242)
        """
    )
    parser.add_argument(
        '-d', '--device',
        help='Input device to use (path, index, or name pattern)',
        default=None
    )
    parser.add_argument(
        '--list-devices',
        action='store_true',
        help='List available input devices and exit'
    )

    args = parser.parse_args()

    # Get device spec from args or environment variable
    device_spec = args.device or os.getenv('FS42_INPUT_DEVICE')

    print(f"Remote Controller started. Connecting to {FS42_BASE_URL}")

    # If --list-devices, just show devices and exit
    if args.list_devices:
        find_input_devices(device_spec)
        return

    # Find input device
    if device_spec == "test":
        test_mode()
        return

    print("Press ESC to exit.")

    while True:
        device_paths = find_input_devices(device_spec)
        if not device_paths:
            print("No matching input devices found. Retrying in 5 seconds...")
            time.sleep(5)
            continue

        open_devices = []
        for path in device_paths:
            try:
                open_devices.append(InputDevice(path))
            except Exception as e:
                print(f"Could not open device {path}: {e}")

        if not open_devices:
            print("Could not open any input devices. Retrying in 5 seconds...")
            time.sleep(5)
            continue

        print(f"Listening for remote control commands on {len(open_devices)} device(s):")
        for dev in open_devices:
            print(f"  - {dev.name} ({dev.path})")

        fd_map = {dev.fd: dev for dev in open_devices}

        try:
            while True:
                r, w, x = select.select(list(fd_map.keys()), [], [], 2.0)
                if not r:
                    continue

                for fd in r:
                    dev = fd_map[fd]
                    try:
                        for event in dev.read():
                            if not handle_key_event(event):
                                return
                    except (OSError, IOError) as err:
                        print(f"Device error on {dev.name}: {err}")
                        del fd_map[fd]
                        try:
                            dev.close()
                        except Exception:
                            pass

                if not fd_map:
                    print("All devices disconnected. Re-scanning...")
                    break

        except PermissionError:
            print("Permission denied. Try running with: sudo python3 remote_controller.py")
            sys.exit(1)
        except KeyboardInterrupt:
            print("\nExiting remote controller...")
            sys.exit(0)
        except Exception as e:
            print(f"Error in main event loop: {e}")
            time.sleep(2)



def mpv_command_pressed(action):
    """Send a non-interrupting mpv runtime command to FS42."""
    if not should_allow_press(action):
        return

    endpoint = action.replace("_", "-")

    try:
        response = requests.post(
            f'{FS42_BASE_URL}/player/mpv/{endpoint}',
            timeout=2
        )

        if response.ok:
            print(f"MPV command sent: {action}")
        else:
            print(f"MPV command failed: {action} ({response.status_code})")
    except Exception as e:
        print(f"MPV command error for {action}: {e}")

if __name__ == "__main__":
    main()