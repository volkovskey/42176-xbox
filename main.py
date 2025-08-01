import os
import sys
import asyncio
import time
import logging
from enum import Enum, auto
from collections import deque
import json

SIMULATE_HUB = True  # when True: skip real hub connection, just simulate
ENABLE_RICH_LOG = True  # toggle terminal rich-style live UI

# hide pygame support prompt
os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = '1'

if sys.platform == "win32":
    sys.coinit_flags = 0  # required for bleak on Windows

import pygame
from bleak import BleakScanner, BleakClient
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles
import uvicorn
from uvicorn import Config, Server

# ---------- LOGGING ----------
logger = logging.getLogger("movehub")
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter("[%(asctime)s] %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
handler.setFormatter(formatter)
logger.addHandler(handler)


# ---------- Telemetry backend ----------
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
clients: set[WebSocket] = set()

@app.websocket("/ws/telemetry")
async def telemetry_ws(websocket: WebSocket):
    await websocket.accept()
    clients.add(websocket)
    try:
        while True:
            await asyncio.sleep(1)
    except Exception:
        pass
    finally:
        clients.discard(websocket)

async def broadcast_telemetry(data: dict):
    payload = json.dumps(data)
    stale = []
    for ws in list(clients):
        try:
            await ws.send_text(payload)
        except Exception:
            stale.append(ws)
    for s in stale:
        clients.discard(s)


# ---------- HUB CLASS ----------
class TechnicMoveHub:
    SERVICE_UUID = "00001623-1212-EFDE-1623-785FEABCD123"
    CHAR_UUID = "00001624-1212-EFDE-1623-785FEABCD123"

    def __init__(self, device_name: str):
        self.device_name = device_name
        self.client: BleakClient | None = None
        self._start_time = None
        self.simulate = SIMULATE_HUB

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
        await self.send_data(bytes.fromhex("0d008136115100030000001000"))
        await asyncio.sleep(0.1)
        await self.send_data(bytes.fromhex("0d008136115100030000000800"))
        await asyncio.sleep(0.1)

    async def drive(self, speed=0, angle=0, lights=0x00):
        logger.info(f"[command] drive power={speed} angle={angle} lights=0x{lights:02x}")
        payload = bytearray([
            0x0d, 0x00, 0x81, 0x36, 0x11,
            0x51, 0x00, 0x03, 0x00,
            speed & 0xFF, angle & 0xFF, lights & 0xFF, 0x00
        ])
        await self.send_data(payload)


# ---------- Helpers ----------
DEADZONE_STICK = 12
DEADZONE_TRIGGER = 10

def apply_deadzone(value, deadzone):
    return 0 if abs(value) < deadzone else value

def get_left_joystick(joystick):
    x = round(joystick.get_axis(0) * 100)
    y = -round(joystick.get_axis(1) * 100)
    return apply_deadzone(x, DEADZONE_STICK), apply_deadzone(y, DEADZONE_STICK)

def get_right_joystick(joystick):
    x = round(joystick.get_axis(2) * 100)
    y = -round(joystick.get_axis(3) * 100)
    return apply_deadzone(x, DEADZONE_STICK), apply_deadzone(y, DEADZONE_STICK)

def get_triggers(joystick):
    left_raw = (joystick.get_axis(4) * 100 + 100) / 2
    right_raw = (joystick.get_axis(5) * 100 + 100) / 2
    left = round(apply_deadzone(left_raw, DEADZONE_TRIGGER))
    right = round(apply_deadzone(right_raw, DEADZONE_TRIGGER))
    return left, right

def compute_light_code(is_braking: bool, lights_enabled: bool) -> int:
    if is_braking:
        return 0x01 if lights_enabled else 0x05
    else:
        return 0x00 if lights_enabled else 0x04

BUTTON_MAPPING = {
    "A": lambda j: j.get_button(0),  # full brake
    "B": lambda j: j.get_button(1),
    "X": lambda j: j.get_button(2),  # mode toggle
    "Y": lambda j: j.get_button(3),  # lights toggle
    "LB": lambda j: j.get_button(4),  # gear down
    "RB": lambda j: j.get_button(5),  # gear up
}


# ---------- Gear state ----------
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
def build_status_table(raw, command, connected, simulate, lights_enabled, brake, gear, lights_code,
                       power_sent, instant_power, avg_power, mode):
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

    cmd = f"instant={instant_power:.1f} smoothed={power_sent} angle={command['angle']} lights_code=0x{lights_code:02x}"
    table.add_row("Drive Command", cmd)
    table.add_row("Power Sent", str(power_sent))
    table.add_row("1m Avg Power", f"{avg_power:.1f}")
    table.add_row("Brake Active", str(bool(brake)))
    table.add_row("Lights Enabled", "ON" if lights_enabled else "OFF")
    table.add_row("Current Gear", gear_name(gear))
    table.add_row("Mode", mode)
    conn = "SIMULATED" if simulate else ("Connected" if connected else "Disconnected")
    table.add_row("Hub Status", conn)
    return table


# ---------- Main logic ----------
async def controller_loop():
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

    lights_enabled = True
    toggle_old = False
    gear_idx = 0
    current_gear = GEAR_ORDER[gear_idx]
    throttle_old = 0
    steering_old = 0
    lights_old_code = compute_light_code(False, lights_enabled)
    was_brake = False

    # mode state: Comfort (smoother) vs Sport (sharper)
    mode = "Comfort"
    x_old = 0

    # smoothing & averaging setup
    COMFORT_ALPHA = 0.08
    SPORT_ALPHA = 0.25
    SMOOTH_ALPHA = COMFORT_ALPHA  # start in comfort
    smoothed_power = 0.0
    window = deque()
    WINDOW_SECONDS = 60.0

    polling_interval = 0.01  # seconds

    raw = {
        "left": (0, 0),
        "right": (0, 0),
        "triggers": (0, 0),
        "buttons": {name: 0 for name in BUTTON_MAPPING},
    }
    command = {"speed": 0, "angle": 0, "raw_throttle": 0}

    live_ctx = Live(refresh_per_second=10, transient=False) if ENABLE_RICH_LOG else None
    if ENABLE_RICH_LOG:
        live_ctx.__enter__()

    try:
        while True:
            pygame.event.pump()

            left_x, left_y = get_left_joystick(joystick)
            right_x, right_y = get_right_joystick(joystick)
            left_trigger, right_trigger = get_triggers(joystick)
            button_states = {name: func(joystick) for name, func in BUTTON_MAPPING.items()}

            # mode toggle on X rising edge
            if button_states["X"] and not raw["buttons"].get("X", 0):
                if mode == "Comfort":
                    mode = "Sport"
                    SMOOTH_ALPHA = SPORT_ALPHA
                    logger.info("Switched to Sport mode")
                else:
                    mode = "Comfort"
                    SMOOTH_ALPHA = COMFORT_ALPHA
                    logger.info("Switched to Comfort mode")

            # gear shifting
            if button_states["LB"] and not raw["buttons"].get("LB", 0):
                gear_idx = max(0, gear_idx - 1)
                current_gear = GEAR_ORDER[gear_idx]
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

            # throttle/brake/back logic
            full_brake = False
            raw_throttle = 0

            if right_trigger > 0:
                if left_trigger > 80 or button_states["A"]:
                    full_brake = True
                    raw_throttle = 0
                else:
                    raw_throttle = right_trigger - left_trigger
            else:
                if button_states["A"]:
                    full_brake = True
                    raw_throttle = 0
                else:
                    raw_throttle = -int(left_trigger * 0.4)

            scale = GEAR_THROTTLE_SCALE.get(current_gear, 0.0)
            if full_brake:
                adjusted_speed = 0
            else:
                adjusted_speed = int(raw_throttle * scale)

            steering = left_x

            # apply exponential smoothing based on mode
            smoothed_power = SMOOTH_ALPHA * adjusted_speed + (1 - SMOOTH_ALPHA) * smoothed_power
            power_to_send = int(smoothed_power)

            # sliding window 1-minute average of smoothed power
            now = time.time()
            window.append((now, smoothed_power))
            while window and now - window[0][0] > WINDOW_SECONDS:
                window.popleft()
            avg_power = sum(p for _, p in window) / len(window) if window else 0.0

            # lights toggle
            toggle = button_states["Y"]
            if toggle and not toggle_old:
                lights_enabled = not lights_enabled
                logger.info(f"Lights {'ON' if lights_enabled else 'OFF'}")
            toggle_old = toggle

            brake_active = full_brake
            lights_code = compute_light_code(brake_active, lights_enabled)

            # drive logic using smoothed power
            if brake_active and not was_brake:
                await hub.drive(0, steering, lights_code)
                throttle_old = 0
            if not brake_active and was_brake:
                await hub.drive(power_to_send, steering, lights_code)
            should_drive = (steering != steering_old or power_to_send != throttle_old or lights_code != lights_old_code)
            if should_drive:
                await hub.drive(power_to_send, steering, lights_code)

            throttle_old = power_to_send
            steering_old = steering
            lights_old_code = lights_code
            was_brake = brake_active

            # update raw/command
            raw["left"] = (left_x, left_y)
            raw["right"] = (right_x, right_y)
            raw["triggers"] = (left_trigger, right_trigger)
            raw["buttons"] = {k: int(v) for k, v in button_states.items()}
            command["raw_throttle"] = raw_throttle
            command["speed"] = power_to_send
            command["angle"] = steering

            # build and render table
            table = build_status_table(
                raw=raw,
                command=command,
                connected=bool(hub.client and getattr(hub.client, "is_connected", False)),
                simulate=hub.simulate,
                lights_enabled=lights_enabled,
                brake=brake_active,
                gear=current_gear,
                lights_code=lights_code,
                power_sent=power_to_send,
                instant_power=adjusted_speed,
                avg_power=avg_power,
                mode=mode,
            )
            if ENABLE_RICH_LOG:
                live_ctx.update(Panel(table, title="Gamepad → Vehicle", border_style="green"))
            else:
                logger.info(f"Gear: {gear_name(current_gear)} | Mode: {mode} | Power sent: {power_to_send} | Avg1m: {avg_power:.1f} | Brake: {brake_active}")

            # telemetry broadcast
            telemetry = {
                "power": power_to_send,
                "instant_power": adjusted_speed,
                "avg_power": avg_power,
                "gear": gear_name(current_gear),
                "mode": mode,
                "raw_left_trigger": left_trigger,
                "raw_right_trigger": right_trigger,
                "angle": steering,
                "brake": brake_active,
                "lights": lights_enabled,
                "buttons": raw["buttons"],
                "timestamp": now,
            }
            asyncio.create_task(broadcast_telemetry(telemetry))

            await asyncio.sleep(polling_interval)

    except asyncio.CancelledError:
        pass
    finally:
        await hub.disconnect()
        pygame.quit()
        logger.info("Finished.")
        if ENABLE_RICH_LOG:
            live_ctx.__exit__(None, None, None)


async def main():
    controller = asyncio.create_task(controller_loop())
    config = Config(
        app=app,
        host="0.0.0.0",
        port=8000,
        log_level="warning",
        loop="asyncio",
        lifespan="off"
    )
    server = Server(config)
    web = asyncio.create_task(server.serve())
    await asyncio.gather(controller, web)


if __name__ == "__main__":
    asyncio.run(main())
