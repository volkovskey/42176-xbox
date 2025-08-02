import asyncio
import time
import logging
import sys
from collections import deque
import json

import pygame
from bleak import BleakScanner, BleakClient
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles
from uvicorn import Config, Server

from config import (
    SIMULATE_HUB,
    ENABLE_RICH_LOG,
    DEADZONE_STICK,
    DEADZONE_TRIGGER,
    Mode,
    SMOOTH_ALPHA_ACCEL,
    SMOOTH_ALPHA_BRAKE,
    GEAR_ORDER,
    GEAR_THROTTLE_SCALE,
    REVERSE_SCALE_PER_GEAR,
    Gear,
    compute_light_code,
)

# ---------- logging ----------
logger = logging.getLogger("movehub")
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(
    logging.Formatter("[%(asctime)s] %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
)
logger.addHandler(handler)

# ---------- telemetry backend ----------
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


# ---------- Hub class ----------
class TechnicMoveHub:
    CHAR_UUID = "00001624-1212-EFDE-1623-785FEABCD123"
    LIGHTS_OFF_OFF = 0b100
    LIGHTS_OFF_ON = 0b101
    LIGHTS_ON_ON = 0b000

    def __init__(self, device_name):
        self.device_name = device_name
        self.client = None
        self._start_time = None
        self.simulate = SIMULATE_HUB

    async def calibrate_steering(self):
        # steering calibration sequence
        await self.send_data(bytes.fromhex("0d008136115100030000001000"))
        await asyncio.sleep(0.1)
        await self.send_data(bytes.fromhex("0d008136115100030000000800"))
        await asyncio.sleep(0.1)

    async def scan_and_connect(self):
        if self.simulate:
            logger.info("[SIMULATION] skipping BLE connect")
            self._start_time = time.time()
            return True
        logger.info(f"Searching for Technic Move Hub '{self.device_name}'...")
        try:
            devices = await BleakScanner.discover(timeout=5)
        except Exception as e:
            logger.error(f"BLE scan failed: {e}")
            return False
        target = next((d for d in devices if d.name and self.device_name in d.name), None)
        if not target:
            logger.error("Device not found")
            return False
        self.client = BleakClient(address_or_ble_device=target, pair=True)
        try:
            await self.client.connect()
        except Exception as e:
            logger.error(f"Connection error: {e}")
            return False
        if not getattr(self.client, "is_connected", False):
            logger.error("Failed to connect")
            return False
        try:
            await self.client.pair(protection_level=2)
        except Exception:
            pass
        self._start_time = time.time()
        logger.info("Connected to hub")
        return True

    async def send_data(self, data: bytes):
        if self.simulate:
            return
        if not self.client or not getattr(self.client, "is_connected", False):
            logger.warning("No active BLE client")
            return
        try:
            await self.client.write_gatt_char(self.CHAR_UUID, data)
        except Exception as e:
            logger.error(f"Send failed: {e}")

    async def drive(self, speed=0, angle=0, lights=0x00):
        speed = int(speed)
        angle = int(angle)
        payload = bytearray(
            [
                0x0d,
                0x00,
                0x81,
                0x36,
                0x11,
                0x51,
                0x00,
                0x03,
                0x00,
                speed & 0xFF,
                angle & 0xFF,
                lights & 0xFF,
                0x00,
            ]
        )
        logger.debug(f"drive payload speed={speed} angle={angle} lights=0x{lights:02x}")
        await self.send_data(payload)


# ---------- helpers ----------
def apply_deadzone(value, dz):
    return 0 if abs(value) < dz else value


def scale_steering(raw):
    # 80% stick equals full turn
    limit = 80
    if abs(raw) <= limit:
        return int((raw / limit) * 100)
    return 100 if raw > 0 else -100


def enforce_vehicle_deadzone(power: float, inner_deadzone=10):
    if power == 0:
        return 0
    if abs(power) < inner_deadzone:
        return inner_deadzone if power > 0 else -inner_deadzone
    return power


def gear_name(g):
    return {
        Gear.FIRST: "1st",
        Gear.SECOND: "2nd",
        Gear.THIRD: "3rd",
    }[g]


def build_status_table(
    raw,
    command,
    connected,
    simulate,
    lights_enabled,
    brake,
    gear,
    lights_code,
    power_sent,
    instant_power,
    avg_power_full,
    avg_2min,
    mode,
):
    table = Table(expand=True)
    table.add_column("Metric", no_wrap=True)
    table.add_column("Value", overflow="fold")

    left = f"x={raw['left'][0]} y={raw['left'][1]}"
    right = f"x={raw['right'][0]} y={raw['right'][1]}"
    triggers = f"L={raw['triggers'][0]} R={raw['triggers'][1]}"
    buttons = ", ".join(f"{k}:{int(v)}" for k, v in raw["buttons"].items())

    table.add_row("Left stick", left)
    table.add_row("Right stick", right)
    table.add_row("Triggers", triggers)
    table.add_row("Buttons", buttons if buttons else "(none)")
    cmd = f"instant={instant_power:.1f} smoothed={power_sent} angle={command['angle']} lights=0x{lights_code:02x}"
    table.add_row("Drive Command", cmd)
    table.add_row("Power Sent", str(power_sent))
    table.add_row("Full Avg Power", f"{avg_power_full:.1f}")
    table.add_row("2min Avg Power", f"{avg_2min:.1f}")
    table.add_row("Brake Active", str(bool(brake)))
    table.add_row("Lights Enabled", "ON" if lights_enabled else "OFF")
    table.add_row("Current Gear", gear_name(gear))
    table.add_row("Mode", mode)
    conn = "SIMULATED" if simulate else ("Connected" if connected else "Disconnected")
    table.add_row("Hub Status", conn)
    return table


# ---------- main loop ----------
async def controller_loop():
    device_name = "Technic Move"
    hub = TechnicMoveHub(device_name)

    if SIMULATE_HUB:
        logger.warning("=== SIMULATION MODE: hub not connected ===")

    connected = await hub.scan_and_connect()
    if not connected:
        return

    pygame.init()
    pygame.joystick.init()
    if pygame.joystick.get_count() == 0:
        logger.error("No joystick detected.")
        return
    joystick = pygame.joystick.Joystick(0)
    joystick.init()
    logger.info(f"Joystick: {joystick.get_name()}")

    await hub.calibrate_steering()

    # initial state
    lights_enabled = True
    toggle_old = False
    gear_idx = 0
    current_gear = GEAR_ORDER[gear_idx]
    throttle_old = 0
    steering_old = 0
    lights_old_code = compute_light_code(False, lights_enabled)
    was_brake = False
    current_mode_enum = Mode.COMFORT
    mode = current_mode_enum.value
    smoothed_power = 0.0

    power_history_full = []
    recent_history = deque()

    raw = {"left": (0, 0), "right": (0, 0), "triggers": (0, 0), "buttons": {}}
    command = {"speed": 0, "angle": 0, "raw_throttle": 0}

    live_ctx = Live(refresh_per_second=10, transient=False) if ENABLE_RICH_LOG else None
    if ENABLE_RICH_LOG:
        live_ctx.__enter__()

    try:
        while True:
            pygame.event.pump()

            # sticks with controller deadzone
            left_x = apply_deadzone(round(joystick.get_axis(0) * 100), DEADZONE_STICK)
            left_y = apply_deadzone(-round(joystick.get_axis(1) * 100), DEADZONE_STICK)
            right_x = apply_deadzone(round(joystick.get_axis(2) * 100), DEADZONE_STICK)
            right_y = apply_deadzone(-round(joystick.get_axis(3) * 100), DEADZONE_STICK)

            # raw trigger values [0..100]
            left_trigger_raw = (joystick.get_axis(4) * 100 + 100) / 2
            right_trigger_raw = (joystick.get_axis(5) * 100 + 100) / 2

            # buttons
            buttons = {
                "A": joystick.get_button(0),
                "B": joystick.get_button(1),
                "X": joystick.get_button(2),
                "Y": joystick.get_button(3),
                "LB": joystick.get_button(4),
                "RB": joystick.get_button(5),
            }

            # mode toggle on X rising edge
            if buttons["X"] and not raw["buttons"].get("X", 0):
                current_mode_enum = Mode.SPORT if current_mode_enum == Mode.COMFORT else Mode.COMFORT
                mode = current_mode_enum.value
                logger.info(f"Mode switched to {mode}")

            # gear shifting
            if buttons["LB"] and not raw["buttons"].get("LB", 0):
                gear_idx = max(0, gear_idx - 1)
                current_gear = GEAR_ORDER[gear_idx]
                if current_gear == Gear.FIRST:
                    joystick.rumble(0.1, 0.1, 150)
                elif current_gear == Gear.SECOND:
                    joystick.rumble(0.2, 0.2, 200)
                elif current_gear == Gear.THIRD:
                    joystick.rumble(0.4, 0.4, 250)
                logger.info(f"Gear changed to {gear_name(current_gear)}")
            if buttons["RB"] and not raw["buttons"].get("RB", 0):
                gear_idx = min(len(GEAR_ORDER) - 1, gear_idx + 1)
                current_gear = GEAR_ORDER[gear_idx]
                if current_gear == Gear.FIRST:
                    joystick.rumble(0.1, 0.1, 150)
                elif current_gear == Gear.SECOND:
                    joystick.rumble(0.2, 0.2, 200)
                elif current_gear == Gear.THIRD:
                    joystick.rumble(0.4, 0.4, 250)
                logger.info(f"Gear changed to {gear_name(current_gear)}")

            # ---------- throttle / brake / reverse logic ----------
            full_brake = False
            raw_throttle = 0.0

            forward_input = right_trigger_raw
            brake_input = left_trigger_raw

            if forward_input > DEADZONE_TRIGGER:
                # moving forward, left trigger subtracts as brake
                if brake_input > 95 or buttons["A"]:
                    full_brake = True
                    raw_throttle = 0.0
                else:
                    raw_throttle = forward_input - brake_input
                    if raw_throttle < 0:
                        raw_throttle = 0.0  # do not invert here
            else:
                # no forward: reverse unless full brake by A
                if buttons["A"]:
                    full_brake = True
                    raw_throttle = 0.0
                else:
                    raw_throttle = -1 * brake_input

            # apply gear scaling
            if full_brake:
                adjusted_speed = 0.0
            else:
                if raw_throttle >= 0:
                    adjusted_speed = raw_throttle * GEAR_THROTTLE_SCALE.get(current_gear, 0.0)
                else:
                    adjusted_speed = -1 * raw_throttle * REVERSE_SCALE_PER_GEAR.get(current_gear, 0.0)

            # enforce vehicle inner deadzone: if non-zero but magnitude <10, snap to ±10
            adjusted_speed = enforce_vehicle_deadzone(adjusted_speed, inner_deadzone=10)

            # steering scaled: 80% stick → full turn
            steering = scale_steering(left_x)

            # smoothing: accel vs brake
            if adjusted_speed > smoothed_power:
                alpha = SMOOTH_ALPHA_ACCEL[current_mode_enum]
                smoothed_power += (adjusted_speed - smoothed_power) * alpha
            else:
                alpha = SMOOTH_ALPHA_BRAKE[current_mode_enum]
                smoothed_power += (adjusted_speed - smoothed_power) * alpha
            power_to_send = int(smoothed_power)

            # history for averages
            now = time.time()
            power_history_full.append((now, smoothed_power))
            avg_power_full = (
                sum(p for _, p in power_history_full) / len(power_history_full)
                if power_history_full
                else 0.0
            )
            recent_history.append((now, smoothed_power))
            while recent_history and recent_history[0][0] < now - 120:
                recent_history.popleft()
            avg_2min = (
                sum(p for _, p in recent_history) / len(recent_history) if recent_history else 0.0
            )

            # lights toggle
            if buttons["Y"] and not toggle_old:
                lights_enabled = not lights_enabled
                logger.info(f"Lights set to {lights_enabled}")
            toggle_old = buttons["Y"]

            brake_active = full_brake
            lights_code = compute_light_code(brake_active, lights_enabled)

            # drive logic
            if brake_active and not was_brake:
                await hub.drive(0, steering, lights_code)
                throttle_old = 0
            if not brake_active and was_brake:
                await hub.drive(power_to_send, steering, lights_code)
            should_drive = (
                steering != steering_old
                or power_to_send != throttle_old
                or lights_code != lights_old_code
            )
            if should_drive:
                await hub.drive(power_to_send, steering, lights_code)

            throttle_old = power_to_send
            steering_old = steering
            lights_old_code = lights_code
            was_brake = brake_active

            # update raw/command
            raw["left"] = (left_x, left_y)
            raw["right"] = (right_x, right_y)
            raw["triggers"] = (round(left_trigger_raw), round(right_trigger_raw))
            raw["buttons"] = {k: int(v) for k, v in buttons.items()}
            command["raw_throttle"] = raw_throttle
            command["speed"] = power_to_send
            command["angle"] = steering

            # telemetry payload
            telemetry = {
                "power": power_to_send,
                "instant_power": adjusted_speed,
                "avg_power_full": avg_power_full,
                "avg_2min": avg_2min,
                "gear": gear_name(current_gear),
                "mode": mode,
                "raw_left_trigger": round(left_trigger_raw),
                "raw_right_trigger": round(right_trigger_raw),
                "angle": steering,
                "brake": brake_active,
                "lights": lights_enabled,
                "buttons": raw["buttons"],
                "timestamp": now,
            }
            asyncio.create_task(broadcast_telemetry(telemetry))

            # UI / log
            if ENABLE_RICH_LOG:
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
                    avg_power_full=avg_power_full,
                    avg_2min=avg_2min,
                    mode=mode,
                )
                live_ctx.update(Panel(table, title="Gamepad → Vehicle", border_style="green"))
            else:
                logger.info(
                    f"Gear={gear_name(current_gear)} Mode={mode} Power={power_to_send:.1f} "
                    f"Avg2min={avg_2min:.1f} Brake={brake_active}"
                )

            await asyncio.sleep(0.01)

    except KeyboardInterrupt:
        logger.info("Shutting down")
    finally:
        await hub.drive(0, 0, 0)
        pygame.quit()
        if ENABLE_RICH_LOG:
            live_ctx.__exit__(None, None, None)


# ---------- entrypoint ----------
async def main():
    controller = asyncio.create_task(controller_loop())
    config = Config(
        app=app,
        host="0.0.0.0",
        port=8000,
        log_level="warning",
        loop="asyncio",
        lifespan="off",
    )
    server = Server(config)
    web = asyncio.create_task(server.serve())
    await asyncio.gather(controller, web)


if __name__ == "__main__":
    asyncio.run(main())
