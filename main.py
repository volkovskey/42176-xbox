import os
import sys
import asyncio
import time
import logging
from enum import Enum, auto

SIMULATE_HUB = False  # when True: skip real hub connection, just simulate

# hide pygame support prompt
os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = '1'

if sys.platform == "win32":
    sys.coinit_flags = 0  # required for bleak on Windows

import pygame
from bleak import BleakScanner, BleakClient
from rich.live import Live
from rich.table import Table
from rich.panel import Panel

# ---------- LOGGING ----------
logger = logging.getLogger("movehub")
logger.setLevel(logging.DEBUG)  # change to INFO to reduce verbosity
handler = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter("[%(asctime)s] %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
handler.setFormatter(formatter)
logger.addHandler(handler)


# ---------- HUB CLASS ----------
class TechnicMoveHub:
    SERVICE_UUID = "00001623-1212-EFDE-1623-785FEABCD123"
    CHAR_UUID = "00001624-1212-EFDE-1623-785FEABCD123"

    LIGHTS_OFF_OFF = 0b100
    LIGHTS_OFF_ON = 0b101
    LIGHTS_ON_ON = 0b000

    SC_BUFFER_NO_FEEDBACK = 0x00
    MOTOR_MODE_POWER = 0x00
    END_STATE_BRAKE = 0x01
    ID_LED = 0x00
    IO_TYPE_RGB_LED = 0x00
    LED_MODE_COLOR = 0x00
    LED_MODE_RGB = 0x01

    def __init__(self, device_name: str):
        self.device_name = device_name
        self.client: BleakClient | None = None
        self._start_time = None
        self.simulate = SIMULATE_HUB  # use global flag

    async def scan_and_connect(self) -> bool:
        if self.simulate:
            logger.info(f"[SIMULATION] Skipping scan/connect for '{self.device_name}'.")
            self._start_time = time.time()
            return True

        logger.info("Searching for Technic Move Hub...")
        try:
            devices = await BleakScanner.discover(timeout=5)
        except Exception as e:
            logger.error(f"BLE scan failed: {e}")
            return False

        target = next((d for d in devices if d.name and self.device_name in d.name), None)
        if not target:
            logger.warning(f"Device '{self.device_name}' not found.")
            return False

        logger.info(f"Found device '{target.name}' [{target.address}] — connecting...")
        self.client = BleakClient(address_or_ble_device=target, pair=True)
        try:
            await self.client.connect()
        except Exception as e:
            logger.error(f"Connection error: {e}")
            return False

        if not self.client.is_connected:
            logger.error("Failed to connect: client reports disconnected.")
            return False

        logger.info("Connected. Attempting to pair (protection level 2)...")
        try:
            paired = await self.client.pair(protection_level=2)
            if not paired:
                logger.warning("Pairing did not fully succeed.")
        except Exception as e:
            logger.error(f"Pairing exception: {e}")
            return False

        self._start_time = time.time()
        logger.info("Hub ready.")
        return True

    async def send_data(self, data: bytes):
        elapsed_ms = (time.time() - (self._start_time or time.time())) * 1000
        hex_repr = ' '.join(f"{b:02x}" for b in data)
        if self.simulate:
            logger.debug(f"[SIMULATED → hub] +{elapsed_ms:7.2f}ms | {hex_repr}")
            return

        if not self.client or not self.client.is_connected:
            logger.warning("Attempted to send data with no active BLE connection.")
            return

        try:
            await self.client.write_gatt_char(self.CHAR_UUID, data)
            logger.debug(f"[→ hub] +{elapsed_ms:7.2f}ms | {hex_repr}")
        except Exception as e:
            logger.error(f"Failed to write data: {e}")

    async def disconnect(self):
        if self.simulate:
            logger.info("[SIMULATION] Skipping hub disconnect.")
            return
        if self.client and self.client.is_connected:
            await self.client.disconnect()
            logger.info("Disconnected from hub.")

    async def calibrate_steering(self):
        # steering calibration sequence
        await self.send_data(bytes.fromhex("0d008136115100030000001000"))
        await asyncio.sleep(0.1)
        await self.send_data(bytes.fromhex("0d008136115100030000000800"))
        await asyncio.sleep(0.1)

    async def drive(self, speed=0, angle=0, lights=0x00):
        logger.info(f"[command] drive speed={speed} angle={angle} lights={lights}")
        payload = bytearray([
            0x0d, 0x00, 0x81, 0x36, 0x11,
            0x51, 0x00, 0x03, 0x00,
            speed & 0xFF, angle & 0xFF, lights & 0xFF, 0x00
        ])
        await self.send_data(payload)


# ---------- Gamepad helpers ----------
DEADZONE_STICK = 12
DEADZONE_TRIGGER = 10

def apply_deadzone(value, deadzone):
    return 0 if abs(value) < deadzone else value

def get_left_joystick(joystick):
    x = round(joystick.get_axis(0) * 100)
    y = -round(joystick.get_axis(1) * 100)
    x = apply_deadzone(x, DEADZONE_STICK)
    y = apply_deadzone(y, DEADZONE_STICK)
    return x, y

def get_right_joystick(joystick):
    x = round(joystick.get_axis(2) * 100)
    y = -round(joystick.get_axis(3) * 100)
    x = apply_deadzone(x, DEADZONE_STICK)
    y = apply_deadzone(y, DEADZONE_STICK)
    return x, y

def get_triggers(joystick):
    # map axis in [-1,1] to [0,100]
    left_raw = (joystick.get_axis(4) * 100 + 100) / 2
    right_raw = (joystick.get_axis(5) * 100 + 100) / 2
    left = round(apply_deadzone(left_raw, DEADZONE_TRIGGER))
    right = round(apply_deadzone(right_raw, DEADZONE_TRIGGER))
    return left, right

# Buttons mapping
BUTTON_MAPPING = {
    "A": lambda j: j.get_button(0),  # full brake
    "B": lambda j: j.get_button(1),
    "X": lambda j: j.get_button(2),
    "Y": lambda j: j.get_button(3),  # lights toggle
    "LB": lambda j: j.get_button(4),  # gear down
    "RB": lambda j: j.get_button(5),  # gear up
}


# ---------- Gear state (no reverse, no neutral) ----------
class Gear(Enum):
    FIRST = auto()
    SECOND = auto()
    THIRD = auto()

GEAR_ORDER = [Gear.FIRST, Gear.SECOND, Gear.THIRD]

GEAR_THROTTLE_SCALE = {
    Gear.FIRST: 0.25,
    Gear.SECOND: 0.5,
    Gear.THIRD: 1.0,
}

def gear_name(g: Gear):
    return {
        Gear.FIRST: "1st",
        Gear.SECOND: "2nd",
        Gear.THIRD: "3rd",
    }[g]


# ---------- UI / Status Table ----------
def build_status_table(raw, command, connected, simulate, lights, brake, gear):
    table = Table(expand=True)
    table.add_column("Category", no_wrap=True)
    table.add_column("Value", overflow="fold")

    left = f"x={raw['left'][0]} y={raw['left'][1]}"
    right = f"x={raw['right'][0]} y={raw['right'][1]}"
    triggers = f"L={raw['triggers'][0]} R={raw['triggers'][1]}"
    buttons = ", ".join(f"{k}:{int(v)}" for k, v in raw["buttons"].items())

    table.add_row("Left stick", left)
    table.add_row("Right stick", right)
    table.add_row("Triggers", triggers)
    table.add_row("Buttons", buttons if buttons else "(none)")

    cmd = f"raw_throttle={command['raw_throttle']} adjusted_speed={command['speed']} angle={command['angle']} lights={command['lights']}"
    table.add_row("Drive Command", cmd)
    table.add_row("Brake Active", str(bool(brake)))
    table.add_row("Lights Mode", "ON" if lights == TechnicMoveHub.LIGHTS_ON_ON else "OFF" if lights == TechnicMoveHub.LIGHTS_OFF_OFF else hex(lights))
    table.add_row("Current Gear", gear_name(gear))
    conn = "SIMULATED" if simulate else ("Connected" if connected else "Disconnected")
    table.add_row("Hub Status", conn)

    return table


# ---------- Main logic ----------
async def main():
    device_name = "Technic Move"
    hub = TechnicMoveHub(device_name)

    if SIMULATE_HUB:
        logger.warning("=== SIMULATION MODE: hub not connected, only logging ===")

    connected = await hub.scan_and_connect()
    if not connected:
        logger.error("Hub not found or failed to connect. Exiting.")
        return

    pygame.init()
    pygame.joystick.init()

    if pygame.joystick.get_count() == 0:
        logger.error("No joystick detected.")
        return

    joystick = pygame.joystick.Joystick(0)
    joystick.init()
    logger.info(f"Joystick name: {joystick.get_name()}")

    await hub.calibrate_steering()

    # initial state
    lights = hub.LIGHTS_ON_ON
    toggle_old = False
    gear_idx = 0
    current_gear = GEAR_ORDER[gear_idx]

    throttle_old = 0
    steering_old = 0
    lights_old = lights
    was_brake = False

    polling_interval = 0.05  # seconds

    raw = {
        "left": (0, 0),
        "right": (0, 0),
        "triggers": (0, 0),
        "buttons": {name: 0 for name in BUTTON_MAPPING},
    }
    command = {"speed": 0, "angle": 0, "lights": lights, "raw_throttle": 0}

    with Live(refresh_per_second=10, transient=False) as live:
        try:
            while True:
                pygame.event.pump()

                left_x, left_y = get_left_joystick(joystick)
                right_x, right_y = get_right_joystick(joystick)
                left_trigger, right_trigger = get_triggers(joystick)
                button_states = {name: func(joystick) for name, func in BUTTON_MAPPING.items()}

                # exit if both sticks pushed near extremes simultaneously
                left_stick_full = abs(left_x) >= (100 - DEADZONE_STICK) or abs(left_y) >= (100 - DEADZONE_STICK)
                right_stick_full = abs(right_x) >= (100 - DEADZONE_STICK) or abs(right_y) >= (100 - DEADZONE_STICK)
                if left_stick_full and right_stick_full:
                    logger.info("Both sticks fully engaged simultaneously; exiting program.")
                    break

                # gear shifting: LB = down, RB = up within bounds
                if button_states["LB"] and not raw["buttons"].get("LB", 0):
                    gear_idx = max(0, gear_idx - 1)
                    current_gear = GEAR_ORDER[gear_idx]
                    # vibration feedback per gear
                    if current_gear == Gear.FIRST:
                        joystick.rumble(0.1, 0.1, 150)
                    elif current_gear == Gear.SECOND:
                        joystick.rumble(0.2, 0.2, 200)
                    elif current_gear == Gear.THIRD:
                        joystick.rumble(0.4, 0.4, 250)
                    logger.info(f"Gear changed to {gear_name(current_gear)}")
                if button_states["RB"] and not raw["buttons"].get("RB", 0):
                    gear_idx = min(len(GEAR_ORDER) - 1, gear_idx + 1)
                    current_gear = GEAR_ORDER[gear_idx]
                    if current_gear == Gear.FIRST:
                        joystick.rumble(0.1, 0.1, 150)
                    elif current_gear == Gear.SECOND:
                        joystick.rumble(0.2, 0.2, 200)
                    elif current_gear == Gear.THIRD:
                        joystick.rumble(0.4, 0.4, 250)
                    logger.info(f"Gear changed to {gear_name(current_gear)}")

                # throttle/brake/back logic:
                full_brake = False
                raw_throttle = 0

                if right_trigger > 0:
                    # gas pressed: left trigger subtracts (brake); full brake if left >80 or A pressed
                    if left_trigger > 80 or button_states["A"]:
                        full_brake = True
                        raw_throttle = 0
                    else:
                        raw_throttle = right_trigger - left_trigger
                else:
                    # no gas: left trigger acts as backward up to 40%; 
                    # brake ONLY by button A (left_trigger >80 більше не гальмує)
                    if button_states["A"]:
                        full_brake = True
                        raw_throttle = 0
                    else:
                        raw_throttle = -int(left_trigger * 0.4)

                # apply gear scaling (full brake overrides)
                scale = GEAR_THROTTLE_SCALE.get(current_gear, 0.0)
                if full_brake:
                    adjusted_speed = 0
                else:
                    adjusted_speed = int(raw_throttle * scale)

                steering = left_x  # steering from left stick X

                # lights toggle on Y press (independent of direction)
                toggle = button_states["Y"]
                if toggle and not toggle_old:
                    if lights == hub.LIGHTS_OFF_OFF:
                        lights = hub.LIGHTS_ON_ON
                        logger.info("Lights turned ON")
                    else:
                        lights = hub.LIGHTS_OFF_OFF
                        logger.info("Lights turned OFF")
                toggle_old = toggle

                brake_active = full_brake

                # drive command logic
                if brake_active and not was_brake:
                    await hub.drive(0, steering, hub.LIGHTS_OFF_ON)
                    throttle_old = 0

                if not brake_active and was_brake:
                    await hub.drive(adjusted_speed, steering, lights)

                should_drive = (
                    (steering != steering_old or adjusted_speed != throttle_old or lights != lights_old)
                )
                if should_drive:
                    await hub.drive(adjusted_speed, steering, lights)

                throttle_old = adjusted_speed
                steering_old = steering
                lights_old = lights
                was_brake = brake_active

                # update raw and command for UI
                raw["left"] = (left_x, left_y)
                raw["right"] = (right_x, right_y)
                raw["triggers"] = (left_trigger, right_trigger)
                raw["buttons"] = {k: int(v) for k, v in button_states.items()}

                command["raw_throttle"] = raw_throttle
                command["speed"] = adjusted_speed
                command["angle"] = steering
                command["lights"] = lights

                # build and refresh status table
                table = build_status_table(
                    raw=raw,
                    command=command,
                    connected=bool(hub.client and getattr(hub.client, "is_connected", False)),
                    simulate=hub.simulate,
                    lights=lights,
                    brake=brake_active,
                    gear=current_gear,
                )
                live.update(Panel(table, title="Gamepad → Vehicle", border_style="green"))

                await asyncio.sleep(polling_interval)

        except KeyboardInterrupt:
            logger.info("Received KeyboardInterrupt, shutting down.")
        finally:
            await hub.disconnect()
            pygame.quit()
            logger.info("Finished.")


if __name__ == "__main__":
    asyncio.run(main())
