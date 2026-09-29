# Cat Talker - Project Summary

## Project Overview
A local, privacy-first desktop AI assistant with real-time voice, vision, and OS control capabilities. Runs entirely on your PC using Google's Gemini Multimodal Live API.

## Current Architecture

### Core Components
| File | Purpose |
|------|---------|
| `src/cat_talker/main.py` | PyQt6 floating UI with animated cat avatar + transcription display + history logger |
| `src/cat_talker/agent.py` | Gemini Live orchestration, combining modular audio, vision, and tools. |
| `src/cat_talker/audio.py` | Audio capture/playback using PyAudio, and volume tracking |
| `src/cat_talker/vision.py` | Screen capture using `grim` (Wayland native) or `mss` (X11) with multi-monitor support |
| `src/cat_talker/tools.py` | OS automation functions (`ydotool`, `xdg-open`, PipeWire, `grim`) |
| `src/cat_talker/config.py` | Settings persistence (API key, preferences) |
| `src/cat_talker/logging_config.py` | Structured logging configuration |
| `pyproject.toml` | Dependencies & project config |

### Technology Stack
- **Voice**: `pyaudio` (16kHz in / 24kHz out) + Gemini Live WebSocket with `asyncio.Lock`
- **Vision**: `grim` (Wayland native) OR `mss` (X11) → 1024x[proportional] JPEG on-demand
- **OS Control**: `ydotool` + `subprocess` + `shutil.which` + `wpctl` + `brightnessctl`
- **UI**: `PyQt6` frameless transparent overlay with dynamic QPainter animations
- **API**: `google-genai` SDK, model `gemini-3.1-flash-live-preview`
- **Logging**: Structured logging with configurable levels (DEBUG/INFO/WARNING/ERROR)
- **Reconnection**: Exponential backoff with jitter (max 60s)

## Implemented Features

### ✅ Core Capabilities
- **Full Duplex Voice** - Bi-directional real-time audio with VAD
- **Wayland Native Vision** - Zero-hallucination desktop capture via `grim`
- **Multi-Monitor Support** - Target specific monitors via `hyprctl`/`mss` (focused, all, or by name/ID)
- **Auto-Reconnect with Memory** - Survives proxy drops with API state-recovery prompt injection + exponential backoff
- **Floating UI** - Draggable cat avatar with live transcriptions
- **Avatar Animation** - Chibi blinks randomly, has blushing cheeks, breathing animation, mouth syncs to volume
- **Conversation Logging** - Permanently backs up chats to `~/.cat_talker_history.txt`
- **Settings Persistence** - API key, preferred monitor, auto-reconnect, voice approval saved to `~/.config/cat-talker/config.json`
- **Modular Design** - Cleanly separated audio, vision, config, logging, and AI modules
- **Structured Logging** - All modules use structured logging (DEBUG/INFO/WARNING/ERROR)
- **Unit Tests** - 9 tests covering config, vision, tools

### ✅ OS Tools (Auto-Execute - No Approval)
| Tool | Description |
|------|-------------|
| `open_application(app)` | Launch any Linux app (maps: brave→brave-browser, terminal→kitty) |
| `open_website(url)` | Open URLs via `xdg-open` or `brave-browser` |
| `get_active_window()` | Read focused window via `hyprctl` (Wayland) |
| `get_clipboard()` | Read clipboard via `wl-paste` or `xclip` |
| `set_volume(level)` | Adjust system audio volume via `wpctl` |
| `set_brightness(level)` | Adjust monitor brightness via `brightnessctl` |
| `list_directory(path)` | See contents of local directories |
| `open_file(path)` | Open specific files using `xdg-open` |
| `search_and_play_youtube(q)` | Natively grabs first YouTube result for a query and autoplays it |
| `take_screenshot(name, monitor?)` | Captures specific monitor or full desktop via `grim`/`mss` to `~/Pictures/Screenshots` |
| `inspect_screen(query, monitor?)` | On-demand vision - captures frame and sends to Gemini for analysis |
| `focus_or_launch(app)` | Focus existing window or launch app (Hyprland) |
| `switch_workspace(num)` | Switch Hyprland workspace (1-10) |
| `media_action(cmd)` | Control media via `playerctl` (play, pause, next, previous, status, metadata) |
| `set_clipboard(text)` | Set system clipboard via `wl-copy` |
| `send_notification(title, body)` | Desktop notification via `notify-send` |
| `save_user_preference(key, value)` | Persistent user memory to `~/.config/cat-talker/memory.json` |

### ⚠️ Risky Tools (Voice Confirmation Required)
| Tool | Description | Confirmation |
|------|-------------|--------------|
| `click_screen(x, y)` | Mouse click (pixel-perfect) via `ydotool` | Voice confirmation ("yes") |
| `type_text("text")` | Type text into focused app via `ydotool` | Voice confirmation ("yes") |
| `press_key(key)` | Press keyboard key (e.g., 'enter') via `ydotool` | Voice confirmation ("yes") |
| `confirm_action()` | Execute pending risky action after user says "yes" | - |
| `cancel_action()` | Cancel pending risky action | - |

*Note: Voice confirmation is implemented natively in `tools.py` by caching the tool payload for 120 seconds and requiring the model to ask the user out loud. The user's vocal "yes" naturally leads the model to execute the `confirm_action` tool.*

## Project Structure
```
cat-talker/
├── src/cat_talker/
│   ├── __init__.py
│   ├── main.py              # PyQt6 UI + Animations + History Logging
│   ├── agent.py             # Gemini Live orchestration + exponential backoff reconnect
│   ├── audio.py             # Audio streams + fixed volume calc + idempotent close()
│   ├── vision.py            # Multi-monitor Grim/MSS capture with monitor targeting
│   ├── tools.py             # 22 OS tools with monitor param support
│   ├── config.py            # Settings persistence (JSON)
│   ├── logging_config.py    # Structured logging setup
│   └── tests/
│       └── test_core.py     # 9 unit tests (config, vision, tools)
├── tests/
│   └── test_core.py
├── pyproject.toml
├── .venv/                   # Virtual environment
└── PROJECT_SUMMARY.md
```

## Dependencies (pyproject.toml)
```toml
google-genai>=1.33.0
pyaudio>=0.2.14
mss>=9.0.2
opencv-python>=4.11.0
PyQt6>=6.8.1
pyautogui>=0.9.54  # pulls ydotool deps
```
*Requires `grim` system package for Wayland screen capture, `brightnessctl` for brightness, `ydotool` + `ydotoold` for input simulation.*

## How to Run
```fish
cd ~/cat-talker
source .venv/bin/activate.fish
sudo systemctl start ydotoold 2>/dev/null || ydotoold &
export GEMINI_API_KEY=your_key_here  # Or set once, saved to config
python -m cat_talker.main
```

Optional: `export LOG_LEVEL=DEBUG` for verbose logging.

## Test Results
```
============================= test session starts ==============================
collected 9 items

tests/test_core.py::test_config_load_save PASSED
tests/test_core.py::test_config_auto_reconnect PASSED
tests/test_core.py::test_config_voice_approval PASSED
tests/test_core.py::test_config_preferred_monitor PASSED
tests/test_core.py::test_vision_interface_initialization PASSED
tests/test_core.py::test_vision_capture_frame PASSED
tests/test_core.py::test_tool_map_complete PASSED (22 tools)
tests/test_core.py::test_take_screenshot_function PASSED
tests/test_core.py::test_inspect_screen_function PASSED

========================= 9 passed in 0.69s ===========================
```

## Known Issues / Next Steps

### 🔴 Critical
- **ydotool on Wayland**: Requires `ydotoold` daemon running + user in `input` group
- **Proxy API Key**: Current key (`AQ.Ab8...`) is a dev proxy, not native Google AI Studio key.

### 🟡 Enhancements Needed
1. **Wake Word Detection** - Replace hotkey with `openwakeword` for hands-free activation
2. **System Tray** - Minimize to tray, show status, quick actions
3. **Settings UI** - In-app configuration panel (hotkeys, monitor, voice, approvals)
4. **API Key Encryption** - Encrypt stored API key (currently plaintext in config.json)
5. **Text Response Modality** - Add `response_modalities=["AUDIO", "TEXT"]` for better transcription

### 🟢 Nice to Have
- **Plugin System** - Dynamic tool loading
- **Multi-language Support** - i18n for UI and prompts
- **Usage Analytics** - Local opt-in telemetry
- **Offline Mode** - Fallback to local LLM (llama.cpp) when API unavailable

---

*Last Updated: 2026-09-23*
*Session: Cat Talker v2.0 - Multi-Monitor Vision, Settings Persistence, Logging, Testing, Bug Fixes*