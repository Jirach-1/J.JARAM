<div align="center">
  <img src="JARAM.ico" alt="JARAM Logo" width="64" height="64">
  <h1>JARAM</h1>
  <p><strong>Just Another Roblox Account Manager</strong></p>
  <p>Manage multiple Roblox accounts with automatic relaunching, server tracking, and optional OCR/automation tooling.</p>
</div>

## Features

### Multi-Account Manager
- Launch and monitor many Roblox clients
- Auto-reconnect on crash/disconnect
- Optional **Spares Mode** to keep standby accounts for fast handoffs
- Window-limit enforcement, orphan cleanup, and an optional age-based kill watchdog (with Discord webhook ping)

### Account + Cookie Tools
- `Accounts` tab plus `File > Manage Users` editor (automatic `users.json` backups)
- Cookie helpers, including **Login with Browser** (Selenium) to extract `.ROBLOSECURITY`
- Supports private servers (link-based) and public places (place ID)

### Private Server Support
- Accepts direct private server links and share links (auto-resolved)
- Supported formats:
  - `https://www.roblox.com/games/[PLACE_ID]/[GAME_NAME]?privateServerLinkCode=[CODE]`
  - `https://www.roblox.com/share?code=[CODE]&type=Server`

### OCR Merchant / Event Detection
- Built-in OCR engine using **RapidOCR (ONNXRuntime)**
- **DirectML** GPU acceleration  with a CPU fallback option
- ROI calibration, color filters, cooldowns, and frame-diff skipping
- Discord webhook alerts for merchants (e.g. **Jester** / **Mari**) with biome + server context

### Multiscope (Server / Biome / Merchant Tracker)
- Groups accounts by the exact server they are in
- Tracks per-server biome (BloxstrapRPC), in-menu state, merchants, and event counts
- Live `Multiscope` tab + persisted all-time counters in `found_stats.json`
- Webhook alerts with per-event rate limiting and custom ping targets

### Anti AFK Engine
- Per-account actions (`space`, `ws`, `zoom`, `AutoReconnect`)
- Configurable key delay (ms) and optional main-menu AutoReconnect

### Auto Item Automation
- Macro-style item use (per-item cooldowns + optional biome restrictions + per-user targeting)
- Coordinate capture, optional conditional click (pixel color match)
- Global hotkey toggle and a "Test once" runner
- Integrates with BES to temporarily unthrottle during actions

### Resource Controls
- `Trimmer` tab: periodically trims Roblox working-set (optional threshold)
- `BES` tab: CPU throttling via Battle Encoder Shirase (menu vs in-game, exempt users)

### Extras Menu Tools
- **Found Stats...**: all-time and time-window biome/merchant totals
- **RAM Export**: import accounts from Roblox Account Manager into `users.json`
- **Utilities**: block/unblock tools + private server link (PSL) grabber

## Quick Start

### Run From Source (Windows)
1. Install **Python 3.14 (64-bit)**.
2. Install dependencies: `pip install -r requirements.txt`
3. Build the native modules with the same interpreter (Visual Studio Build Tools required):
   - `cd native`
   - `python -m pip install -U "pybind11>=3.0" "setuptools>=77"`
   - `python setup.py build_ext --inplace`
   - `cd ..`
4. Run: `python gui.py`

The `.pyd` ABI tag must match Python. Python 3.14 loads the `cp314` builds; it
will intentionally ignore older `cp312` files. See `native/NATIVE_BUILD.md` for
build and verification details.

### System Requirements
- **Operating System**: Windows 10/11
- **Python**: 3.14, 64-bit (if running from source)
- **Roblox**: Installed and working on the system
- **Optional**:
  - Chrome for browser cookie login + utilities


## Configuration Location

All configuration files are stored in `%APPDATA%\\JARAM\\` (use `File > Show Config Location`):
- `users.json` - accounts + per-user metadata
- `settings.json` - application settings
- `backups\\` - automatic timestamped backups
- `found_stats.json` - all-time biome/merchant counters (Multiscope)
- `block_log.json`, `users_to_block.txt` - Utilities state

## Usage Notes

### Adding Accounts
- Use the `Accounts` tab or `File > Manage Users`.
- Cookies:
  - Manual: copy `.ROBLOSECURITY` from browser DevTools.
  - Built-in: click **Login with Browser** (requires Chrome + Selenium).

### OCR Setup
1. Open the `OCR` tab and enable OCR.
2. Calibrate ROI (chat area), adjust color filters and cooldowns.
3. If you see "DirectML provider is not available", install `onnxruntime-directml` or switch OCR device to CPU.

### RAM (Roblox Account Manager) Import
- Use `Extras > RAM Export` to fetch accounts from RAM via its HTTP API and merge/replace `users.json`.

### Utilities (Blocking/Unblocking/PSL Grabber)
- Use `Extras > Utilities`.
- Utilities run actions using your stored cookies; do not share `users.json`.

## Troubleshooting
- Input automation not working (Auto Item / Anti AFK): try running JARAM as Administrator and avoid overlays that block foreground input.
- "Login with Browser" fails: ensure Chrome is installed and dependencies installed (`pip install -r requirements.txt`).

## License and Disclaimer

This project is developed for educational and personal use purposes. Please refer to [LICENSE](LICENSE.md) for terms. You are responsible for:
- Complying with Roblox Terms of Service
- Ensuring account security and cookie protection

We are not responsible for misuse of this application or violations of service terms.

## Support and Community

- Discord: https://discord.gg/6cuCu6ymkX

### Reporting Issues
When reporting issues, please include:
- Windows version
- Python version (if running from source)
- Logs from the `Logs` tab
- Steps to reproduce
- Configuration details (do not include cookies)
