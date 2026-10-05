# ARCHIVED HISTORICAL PLAN (project-start era; all items below are long complete)

> This document is preserved as history. It describes the very first
> bug-fix plan from before checkpoint `5ee950c`. Current state, roadmap,
> and test counts live in `PROJECT_STATUS.md`, `ROADMAP.md`, and
> `README.md` (374 tests passing). Do not treat unchecked boxes below
> as open work.

# Implementation Plan: Cat Talker Bug Fixes & Improvements

## Overview
This plan addresses critical bugs, missing features, and architectural improvements for the Cat Talker desktop AI assistant. The project uses Gemini Live API for real-time voice/vision with OS control capabilities.

## Architecture Decisions
- Keep modular architecture (main.py, agent.py, audio.py, vision.py, tools.py)
- Use PyQt6 for UI with programmatic drawing (no sprite files needed - they're unused)
- Maintain Wayland-native approach with grim/ydotool
- Keep voice confirmation flow for risky actions (click, type, press_key)

## Task List

### Phase 1: Critical Bug Fixes

#### Task 1: Fix Missing `__init__.py` in src/cat_talker/
**Description:** The package is missing `__init__.py`, which breaks `python -m cat_talker.main` imports.

**Acceptance criteria:**
- [ ] Create `src/cat_talker/__init__.py` with proper exports
- [ ] `python -m cat_talker.main` runs without import errors

**Verification:**
- [ ] Run: `cd /home/guru/cat-talker && python -m cat_talker.main` (check import works)

**Dependencies:** None

**Files likely touched:**
- `src/cat_talker/__init__.py`

**Estimated scope:** XS

---

#### Task 2: Fix Vision-on-Demand Integration (inspect_screen tool)
**Description:** The `inspect_screen` tool returns "SCREEN_INSPECT_REQUESTED" magic string but agent.py doesn't handle it to actually capture and send a frame to Gemini.

**Acceptance criteria:**
- [ ] When `inspect_screen` is called, agent captures frame via `vision.capture_frame()`
- [ ] Frame is sent to Gemini via `session.send_realtime_input()` with video modality
- [ ] Model receives and can analyze the screenshot

**Verification:**
- [ ] Test: Ask Chibi "what's on my screen?" - should capture and analyze

**Dependencies:** Task 1

**Files likely touched:**
- `src/cat_talker/agent.py` (receive_worker tool handling)

**Estimated scope:** M

---

#### Task 3: Fix Audio Volume Calculation Bug
**Description:** Volume calculation in `audio.py` uses incorrect formula: `min(1.0, (rms ** 0.5) / 32768.0 * 20.0)` - the `* 20.0` makes volume exceed 1.0 and scaling is wrong.

**Acceptance criteria:**
- [ ] Volume callback receives values 0.0-1.0 representing normalized amplitude
- [ ] Cat mouth animation responds correctly to speech volume

**Verification:**
- [ ] Run app, speak - mouth should open proportionally to volume

**Dependencies:** Task 1

**Files likely touched:**
- `src/cat_talker/audio.py` (lines 76-85)

**Estimated scope:** XS

---

#### Task 4: Fix Take Screenshot Tool Integration with Vision
**Description:** `take_screenshot` tool saves file via grim but doesn't return the captured frame to Gemini for analysis. The agent has a comment about "re-sending screenshot" but it's not implemented.

**Acceptance criteria:**
- [ ] After `take_screenshot` executes, captured frame is sent to Gemini
- [ ] Model can describe what was in the screenshot

**Verification:**
- [ ] Ask Chibi to take screenshot and describe it

**Dependencies:** Task 2

**Files likely touched:**
- `src/cat_talker/agent.py` (tool response handling)
- `src/cat_talker/tools.py` (take_screenshot function)

**Estimated scope:** S

---

#### Task 5: Fix Audio Stream Cleanup on Reconnection
**Description:** When agent reconnects (proxy drops), old audio streams aren't properly closed, causing resource leaks and potential "device busy" errors.

**Acceptance criteria:**
- [ ] AudioInterface.close() called before creating new AudioInterface on reconnect
- [ ] No "device busy" errors after multiple reconnections

**Verification:**
- [ ] Simulate 3+ reconnects, verify no audio errors

**Dependencies:** Task 1

**Files likely touched:**
- `src/cat_talker/agent.py` (reconnection logic in run_loop)
- `src/cat_talker/audio.py` (ensure close() is idempotent)

**Estimated scope:** S

---

### Checkpoint: Critical Bugs Fixed
- [ ] All imports work
- [ ] Vision on demand works
- [ ] Volume calculation correct
- [ ] Screenshots analyzed by model
- [ ] Reconnection stable

---

### Phase 2: Feature Completion

#### Task 6: Implement Multi-Monitor Support for Vision
**Description:** Currently `grim` captures all monitors. Need to parse `hyprctl monitors` to target specific monitor.

**Acceptance criteria:**
- [ ] VisionInterface detects all monitors via `hyprctl monitors -j`
- [ ] `capture_frame()` accepts optional `monitor_index` parameter
- [ ] Tools can specify which monitor to capture

**Verification:**
- [ ] Test on multi-monitor setup - capture specific monitor

**Dependencies:** Task 2

**Files likely touched:**
- `src/cat_talker/vision.py`
- `src/cat_talker/tools.py` (take_screenshot, inspect_screen)

**Estimated scope:** M

---

#### Task 7: Add Settings Persistence (API Key, Preferences)
**Description:** API key and user preferences should be saved to config file instead of requiring env var each run.

**Acceptance criteria:**
- [ ] Config file at `~/.config/cat-talker/config.json`
- [ ] Stores: api_key, preferred_monitor, auto_reconnect, voice_approval_enabled
- [ ] Loads on startup, falls back to env var

**Verification:**
- [ ] Run without GEMINI_API_KEY env var after first setup - works

**Dependencies:** Task 1

**Files likely touched:**
- `src/cat_talker/main.py` (startup logic)
- `src/cat_talker/agent.py` (API key handling)
- New: `src/cat_talker/config.py`

**Estimated scope:** S

---

#### Task 8: Add Wake Word Detection (Optional Enhancement)
**Description:** Replace hotkey activation with `openwakeword` for hands-free activation.

**Acceptance criteria:**
- [ ] Optional dependency: `openwakeword`
- [ ] Detects "Hey Chibi" or similar wake word
- [ ] Starts listening session on detection

**Verification:**
- [ ] Say wake word - cat starts listening

**Dependencies:** Phase 1 complete

**Files likely touched:**
- `src/cat_talker/audio.py` (add wake word processing)
- `src/cat_talker/main.py` (UI state for wake word)
- `pyproject.toml` (optional dependency)

**Estimated scope:** M

---

### Checkpoint: Features Complete
- [ ] Multi-monitor vision works
- [ ] Settings persist across restarts
- [ ] (Optional) Wake word works

---

### Phase 3: Code Quality & Polish

#### Task 9: Remove Unused Sprite Generation Code
**Description:** `generate_placeholders.py` and `assets/sprites/` are unused - main.py draws cat programmatically. Clean up.

**Acceptance criteria:**
- [ ] Remove `generate_placeholders.py`
- [ ] Remove `assets/sprites/` directory
- [ ] Remove `assets/characters/` directory (unused)
- [ ] Keep only `assets/icons/` if used

**Verification:**
- [ ] App runs identically
- [ ] `git status` shows only expected deletions

**Dependencies:** None

**Files likely touched:**
- `generate_placeholders.py` (delete)
- `assets/sprites/` (delete)
- `assets/characters/` (delete)

**Estimated scope:** XS

---

#### Task 10: Add Error Handling & Logging
**Description:** Improve error handling - add structured logging, better error messages in UI.

**Acceptance criteria:**
- [ ] Replace `print()` with `logging` module
- [ ] Log levels: DEBUG, INFO, WARNING, ERROR
- [ ] UI shows connection status clearly
- [ ] Errors don't crash the app silently

**Verification:**
- [ ] Run with `LOG_LEVEL=DEBUG` - see structured logs
- [ ] Disconnect network - UI shows "reconnecting"

**Dependencies:** Task 1

**Files likely touched:**
- All Python files (add logging)
- `src/cat_talker/main.py` (connection status UI)

**Estimated scope:** M

---

#### Task 11: Add Unit Tests for Core Functions
**Description:** Add basic tests for tools, audio, vision modules.

**Acceptance criteria:**
- [ ] Test file: `tests/test_tools.py` - test tool functions
- [ ] Test file: `tests/test_audio.py` - test volume calculation
- [ ] Test file: `tests/test_vision.py` - test frame capture (mocked)
- [ ] Run with `pytest`

**Verification:**
- [ ] `pytest tests/` passes

**Dependencies:** Task 1

**Files likely touched:**
- `tests/` (new directory)
- `pyproject.toml` (add pytest)

**Estimated scope:** M

---

#### Task 12: Fix Type Hints & Add Docstrings
**Description:** Add proper type hints and Google-style docstrings to all public functions.

**Acceptance criteria:**
- [ ] All public functions have type hints
- [ ] All public functions have docstrings
- [ ] `mypy` passes (if added)

**Verification:**
- [ ] Run mypy on src/

**Dependencies:** Task 1

**Files likely touched:**
- All `.py` files in `src/cat_talker/`

**Estimated scope:** M

---

### Checkpoint: Code Quality
- [ ] Unused code removed
- [ ] Logging implemented
- [ ] Tests passing
- [ ] Type hints complete

---

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Gemini API changes breaking Live API | High | Pin google-genai version, test after updates |
| ydotool permission issues on Wayland | High | Document `input` group requirement clearly |
| Audio device conflicts on reconnect | Medium | Ensure proper cleanup in Task 5 |
| Vision capture failures on some Wayland compositors | Medium | Fallback to mss, log clear errors |
| API key proxy vs native key confusion | Medium | Task 7 config handles both |

---

## Open Questions
1. Should we support X11 as primary or keep Wayland-first?
2. Is the dev proxy API key acceptable long-term or must we migrate to native Google AI Studio?
3. Should wake word be mandatory or optional dependency?
4. Do we need a system tray icon for background operation?