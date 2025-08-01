# 42176 XBOX RC

Controller application that maps a gamepad to a LEGO Technic Move Hub (or simulates it) with:
- Gear-based throttle scaling (1st/2nd/3rd)
- Brake and reverse logic
- Light toggle
- Live status display via a terminal table (`rich`)
- Simulation mode for testing without the real hardware

## Features

- **Throttle / Brake / Reverse**:
  - Right trigger: forward throttle.
  - Left trigger: when no forward throttle, acts as reverse up to 40%.
  - Brake: button **A** (always full brake), and when moving forward left trigger >80% subtracts from throttle (and >80% or A triggers full brake).
- **Gears** (only forward):
  - 1st: 25% power
  - 2nd: 50% power
  - 3rd: 100% power
  - Shift with `LB` (down) and `RB` (up); each gear change emits vibration feedback.
- **Lights**: toggle with button `Y` (independent of direction).
- **Exit condition**: pushing both sticks near their extremes simultaneously quits the program.
- **Simulation mode**: skip real BLE connection and just log commands.
- **Live UI**: current gamepad state, command, gear, and hub connection shown in terminal table.

## Requirements

- Python 3.10+ (for union type usage)
- `bleak`
- `pygame`
- `rich`

## Installation

1. Clone the repo:

   ```bash
   git clone https://github.com/volkovskey/42176-xbox
   cd 42176-xbox
   ```

2. (Optional) Create and activate virtual environment:

   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Windows: .venv\Scripts\activate
   ```

3. Install dependencies:

   ```bash
   pip install bleak pygame rich
   ```

## Configuration

Toggle simulation mode at top of script:

```python
SIMULATE_HUB = False  # set to True to run without real hardware
```

## Usage

```bash
python main.py
```

### Controls

- **Right trigger**: forward throttle.
- **Left trigger**: subtracts from throttle when gas present; acts as reverse (max 40%) when no gas.
- **Button A**: full brake.
- **LB / RB**: gear down/up (1st/2nd/3rd).
- **Y**: toggle lights.
- **Both sticks full**: exit.

## Live Status

Shows sticks, triggers, buttons, current drive command, brake state, gear, lights, and hub status.

## Troubleshooting

- Joystick not detected: ensure connected and recognized.
- BLE issues: use simulation mode to isolate.
- Rumble unsupported: some controllers don't expose it via pygame.
- Terminal must support ANSI for live display.

## Enhancements

- Persist settings to config.
- GUI instead of terminal.
- Telemetry export.