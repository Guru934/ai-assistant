# Cat Talker - Project Summary

## Project Overview
A local, privacy-first desktop AI assistant with real-time voice, vision, and OS control capabilities. Runs entirely on your PC using Google's Gemini Multimodal Live API.

## Current Architecture

### Core Components
| File | Purpose |
|------|---------|
| `src/cat_talker/main.py` | PyQt6 floating UI with animated cat avatar + readable wrapping speech bubble + transcription display + history logger + reminder scheduler lifecycle |
| `src/cat_talker/tray.py` | System-tray icon/menu: live status row, Show/Wake/Sleep/Settings, read-only ydotool status dialog, Quit |
| `src/cat_talker/settings.py` | In-app settings panel (API key → OS keyring, monitor, voice, approvals) |
| `src/cat_talker/credentials.py` | SecretService keyring API-key storage + legacy plaintext migration/scrub |
| `src/cat_talker/wakeword.py` | Optional offline openwakeword detection ("Hey Jarvis", off by default) |
| `src/cat_talker/ydotool_health.py` | Read-only ydotool backend health states + setup hints |
| `src/cat_talker/agent.py` | Gemini Live orchestration, combining modular audio, vision, and tools. |
| `src/cat_talker/audio.py` | Audio capture/playback using PyAudio, and volume tracking |
| `src/cat_talker/vision.py` | Screen capture using `grim` (Wayland native) or `mss` (X11) with multi-monitor support |
| `src/cat_talker/tools.py` | OS automation functions (`ydotool`, `xdg-open`, PipeWire, `grim`) + reminders |
| `src/cat_talker/reminders.py` | Validated reminder store + deterministic scheduler (notification delivery only) |
| `src/cat_talker/config.py` | Settings persistence (API key, preferences) |
| `src/cat_talker/logging_config.py` | Structured logging configuration |
| `pyproject.toml` | Dependencies & project config |

### Technology Stack
- **Voice**: `pyaudio` (16kHz in / 24kHz out) + Gemini Live WebSocket with `asyncio.Lock`
- **Vision**: `grim` (Wayland native) OR `mss` (X11) → 1024x[proportional] JPEG on-demand
- **OS Control**: `ydotool` + `subprocess` + `shutil.which` + `wpctl` + `brightnessctl`
- **UI**: `PyQt6` frameless transparent overlay with dynamic QPainter animations
- **API**: `google-genai` SDK, model `gemini-3.8-live`
- **Voice path**: 512-frame (32 ms) 16 kHz mic chunks, explicit Live VAD
  (enabled; HIGH start / LOW end sensitivity, 300 ms prefix, 700 ms end
  silence), `en-IN`/`hi-IN` transcription hints, transcript safety guard
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
- **Unit Tests** - 580 tests covering lifecycle, audio, sleep/wake,
  control/launch, vision, tools, grounding + stale-frame protection +
  multi-step context, web info, Weather, Memory, TTS, assistant text
  stream, coding worker + verification, reminders (store, scheduler,
  firing, contract), workspace switching (1–6),
  auto-hide visibility, tray + bubble UI, wake word, settings, secure
  credentials + migration, ydotool health/setup, diagnostics (see
  "Test Results" below)

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
| `switch_workspace(num)` | Switch Hyprland workspace (user-facing 1–6; input validated/clamped 1–10, success verified) |
| `media_action(cmd)` | Control media via `playerctl` (play, pause, next, previous, status, metadata) |
| `set_clipboard(text)` | Set system clipboard via `wl-copy` |
| `send_notification(title, body)` | Desktop notification via `notify-send` |
| `save_user_preference(key, value)` | Persistent user memory to `~/.config/cat-talker/memory.json` |
| `get_current_datetime()` | Local date/time from the system clock (read-only) |
| `web_search(query)` | Current web search, DuckDuckGo Lite, bounded, honest failures |
| `fetch_webpage(url)` | Page fetch + readable extraction, UNTRUSTED-DATA fenced |
| `get_weather(location, days?)` | Open-Meteo geocode → forecast, °C/km/h |
| `get_preference(key)` | Read an explicitly stored preference (read-only) |
| `set_preference(key, value)` | Explicitly save a preference (validated keys) |
| `delete_preference(key)` | Explicitly delete a preference |
| `read_aloud(text)` | Speak text via local `espeak-ng` through existing playback |
| `run_coding_task(task, workspace)` | Delegate coding work (voice approval; verified reporting) |
| `create_reminder(message, kind, …)` | Explicit reminder request: once (exact aware datetime) or daily (HH:MM); notification only |
| `list_reminders()` | Read-only reminder listing, soonest first |
| `cancel_reminder(id)` | Cancel by existing id (persists across restarts) |

### ⚠️ Risky Tools (Voice Confirmation Required)
| Tool | Description | Confirmation |
|------|-------------|--------------|
| `click_screen(x, y, …)` | Frame-grounded mouse click via `ydotool` (current `frame_seq` required; stale frames refused) | Voice confirmation ("yes") |
| `type_text("text")` | Type text into focused app via `ydotool` | Voice confirmation ("yes") |
| `press_key(key)` | Press keyboard key (e.g., 'enter') via `ydotool` | Voice confirmation ("yes") |
| `confirm_action()` | Execute pending risky action after user says "yes" | - |
| `cancel_action()` | Cancel pending risky action | - |

*Note: Voice confirmation is implemented natively in `tools.py` by caching the tool payload for 120 seconds and requiring the model to ask the user out loud. The user's vocal "yes" naturally leads the model to execute the `confirm_action` tool. Coding delegation (`run_coding_task`) uses the same approval flow.*

### ✅ Later milestones (all implemented, unit-tested)
- **Local date/time, web search + page fetch** (bounded, untrusted-fenced)
- **Weather** (Open-Meteo, explicit place, honest errors)
- **Memory/Preferences** (explicit 5-key validated store; never automatic)
- **Read-aloud TTS fallback** (local engine, replaceable boundary)
- **Sleep/wake hardening** (single mic-worker ownership, serialized
  stream recreation, single-instance launch locking, no headless
  duplicates)
- **Media-aware auto-sleep** (read-only MPRIS `Playing`-edge watcher)
- **Transcript safety guard** + audio/transcript diagnostics
- **External coding worker** (delegated execution + independent
  verification; worker claims ≠ verified facts)
- **Workspace switching** (user-facing 1–6) + **auto-hide avatar UI**
  after successful desktop-opening/focus actions (visibility-only;
  never sleeps)
- **Computer-use grounding + stale-frame protection** (`frame_seq`
  contract, max 2 alternate retries) + **multi-step context**
  (`ComputerUseContext` across inspect/click/verify turns)
- **Assistant text stream** (TEXT from output-audio-transcription
  events; explicit AUDIO+TEXT rejected by the API for this model)
- **System tray** (live status, Show/Wake/Sleep/Settings, ydotool
  dialog, Quit) + **readable speech bubble** (wrapping, never
  clipped, avatar re-layout)
- **Optional local wake word** (openwakeword "Hey Jarvis", off by
  default, fully offline)
- **Settings UI** (key/monitor/voice/approvals; key → OS keyring)
- **Secure API-key storage** (SecretService keyring; legacy plaintext
  auto-migrated + scrubbed; precedence secure → legacy → env)
- **ydotool runtime health/setup** (machine-readable states,
  `ydotool-status`, user-level setup script, one-time `input`-group
  step for reboot-proof startup)
- **Coding-worker reporting** (worker claims vs independently verified
  facts strictly separated in every report)
- **Local reminders** (explicit requests only; once/daily; validated
  JSON store + deterministic scheduler + notification delivery;
  model contract: never invent, read-only list, cancel existing
  only, ask rather than guess)

## Project Structure
```
cat-talker/
├── src/cat_talker/
│   ├── __init__.py
│   ├── main.py              # PyQt6 UI + Animations + History Logging
│   ├── agent.py             # Gemini Live orchestration + exponential backoff reconnect
│   ├── audio.py             # Audio streams + fixed volume calc + idempotent close()
│   ├── vision.py            # Multi-monitor Grim/MSS capture with monitor targeting
│   ├── tools.py             # 34 tools with monitor param support
│   ├── sleep.py             # Sleep/wake state machine + idle policy
│   ├── control.py           # Control socket server (runtime-dir locking)
│   ├── media_watcher.py     # Read-only MPRIS Playing-edge watcher
│   ├── web_search.py        # Current web search (DuckDuckGo Lite)
│   ├── webpage.py           # Page fetch + readable extraction
│   ├── weather.py           # Open-Meteo geocode → forecast
│   ├── memory.py            # Explicit validated preferences
│   ├── speech.py            # Standalone TTS boundary (local engine)
│   ├── coding_worker.py     # Delegated execution + verification
│   ├── echo_suppress.py     # Monitor-reference echo suppression
│   ├── earcons.py           # UI earcons
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
```bash
cd /home/guru/ai-assistant
PYTHONPATH=src .venv/bin/python -m cat_talker.main
# or: export GEMINI_API_KEY=your_key_here  # Or set once via Settings, saved to the OS keyring
bin/assistant-ydotool-setup  # enables the packaged ydotool.service user unit (user level, never sudo)
```

Daily control (any directory, plus Hyprland F1 = UI, F2 = sleep/wake,
F3 = quit):
```bash
/home/guru/ai-assistant/bin/assistant-control toggle
/home/guru/ai-assistant/bin/assistant-control status
```

Optional: `export LOG_LEVEL=DEBUG` for verbose logging.

## Test Results
```bash
./.venv/bin/python -m pytest -q
# 580 passed — no hardware, network, speakers, or credentials needed
# (fakes for Live sessions, audio, vision, Qt offscreen, playerctl,
# subprocess, network). See README "How to run tests" for coverage.
```

## Known Issues / Next Steps

### Current limitations (honest, see README/PROJECT_STATUS.md for detail)
- **Echo suppression/audio isolation** is improved but defensive, not a
  perfect guarantee; live-mic validation across wake cycles is
  real-world-pending.
- **Generic visual clicking** is best-effort (`ydotoold` + `input` group).
- **No wake word unless enabled** (needs `openwakeword` +
  `wake_word_enabled: true`; otherwise F2 wakes); Hyprland needed
  for global keys; needs network + valid Gemini API key.
- **Coding worker has no OS-level sandbox**; worker-reported tests are
  claims until independently verified.
- **`read_aloud` needs `espeak-ng`**; Weather needs network; web
  search/fetch are bounded best-effort; English/Hindi responses only.
- **Reminders fire only while the Chibi process runs**; once/daily
  only, no snooze/edit, notification delivery only (never scheduled
  clicks/keys/shell/coding/web); F2/F3 live-GUI smoke testing not
  performed.

### ✅ Since implemented (were future, now done — kept here for history)
1. **Wake Word Detection** - optional offline `openwakeword` ("Hey
   Jarvis", off by default; F2 remains the fallback)
2. **System Tray** - tray icon with live status, Show/Wake/Sleep,
   Settings, ydotool dialog, Quit
3. **Settings UI** - in-app panel (API key → OS keyring, monitor,
   voice, approvals)
4. **API Key Encryption** - SecretService keyring storage + legacy
   plaintext migration/scrub (no plaintext at rest)
5. **Text Response Modality** - TEXT assembled from
   output-audio-transcription events (explicit AUDIO+TEXT rejected by
   the API for this model; audio path untouched)

### 🟢 Nice to Have
- **Plugin System** - Dynamic tool loading
- **Usage Analytics** - Local opt-in telemetry
- **Offline Mode** - Fallback to local LLM (llama.cpp) when API unavailable

---

*Last verified: 2026-10-06 — automated suite 580 passing at the
`f4514ae` code checkpoint; current GitHub/documentation checkpoint
`99ab236`, which additionally records the live-desktop acceptance
subsequently human-verified PASS (separate from the automated suite).
Live voice-command end-to-end for workspace 5/6 and the auto-hide
round-trip is human-verified PASS on the live desktop (workspace 5/6,
app-open auto-hide, F1 restore, F2 wake/listen, F3 quit; no defects),
in addition to `hyprctl`/tool-level + offscreen Qt coverage.
Historical note (2026-09-23 session): Cat Talker v2.0 brought
multi-monitor vision, settings persistence, logging, early bug fixes —
all of the above supersedes that snapshot.*