from enum import Enum, auto

# Simulation / UI toggles
SIMULATE_HUB = False  # skip real BLE connection if True
ENABLE_RICH_LOG = False  # terminal live table

# Deadzones
DEADZONE_STICK = 8
DEADZONE_TRIGGER = 5 

# Mode definitions
class Mode(Enum):
    COMFORT = "Comfort"
    SPORT = "Sport"

# Smoothing factors per mode (accel vs brake)
SMOOTH_ALPHA_ACCEL = {
    Mode.COMFORT: 0.01,  # slower increase on accel
    Mode.SPORT: 0.15,
}
SMOOTH_ALPHA_BRAKE = {
    Mode.COMFORT: 0.15,  # faster drop on brake
    Mode.SPORT: 0.35,
}

# Gear definitions (only forward)
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

# Reverse scale per gear (negative)
REVERSE_SCALE_PER_GEAR = {
    Gear.FIRST: -0.15,
    Gear.SECOND: -0.25,
    Gear.THIRD: -0.5,
}

def compute_light_code(is_braking: bool, lights_enabled: bool) -> int:
    if is_braking:
        return 0x01 if lights_enabled else 0x05
    else:
        return 0x00 if lights_enabled else 0x04
