# AI Assistant (cat-talker)

A local desktop AI companion for Linux/Hyprland: real-time voice dialogue
with vision on demand and OS control, powered by the Gemini Live API. It
runs as a small floating avatar overlay with global hotkeys for visibility,
sleep/wake, and quit. For clearly coding-oriented requests it can delegate
to an external coding worker in a chosen repository and report back
independently verified results.

## What it currently does

- **Voice conversation** — full-duplex speech with the assistant; speech
  bubble captions and a history log at `~/.cat_talker_history.txt`.
  16 kHz mic capture (512-frame chunks) / 24 kHz output, explicit Live
  VAD, `en-IN`/`hi-IN` transcription hints, and a transcript safety
  guard that keeps short commands working while filtering obvious
  noise.
- **Vision on demand** — `take_screenshot` / `inspect_screen` capture
  (grim on Wayland, mss on X11, multi-monitor aware) and describe or act
  on what is visible.
- **OS control via tools** — 31 tools: open apps/sites/files, clipboard,
  volume, brightness, workspaces, media (`playerctl`), notifications,
  YouTube search-and-play, and preferences. Risky actions
  (`click_screen`, `type_text`, `press_key`, `run_coding_task`) require
  spoken "yes" approval first.
- **Local date/time** — `get_current_datetime` answers "what time is it?",
  "what's today's date?", "what day is today?" from Python's system clock
  (deterministic, timezone-aware, standard library only, no network).
- **Current web information** — `web_search` (DuckDuckGo Lite, max 5
  results, bounded snippets/output, honest failures) plus `fetch_webpage`
  (http(s) only, 10 s timeout, 512 KB cap, max 3 redirects, Content-Type
  gate, max 4000 chars, UNTRUSTED-DATA fencing); max 3 fetches per turn.
- **Weather** — `get_weather` resolves an explicit place via Open-Meteo
  geocoding, then reports current/feels-like/condition/precipitation/wind
  plus highs/lows and multi-day outlook in °C and km/h. Never guesses
  your location; may use a stored `weather_location`.
- **Memory/Preferences** — explicit `get/set/delete_preference` over a
  small validated key set (`preferred_name`, `preferred_language`,
  `temperature_unit`, `weather_location`, `response_style`). Memory is
  explicit, never automatic; nothing is inferred or persisted silently.
- **Read aloud** — `read_aloud` speaks long tool/web text through the
  existing playback path using local `espeak-ng` (honest error with
  install guidance when absent). Normal replies already arrive as
  speech, so this is only for explicit read requests.
- **External coding worker** — `run_coding_task` delegates implementation
  work to an external CLI inside an explicitly selected workspace
  (argv-only, bounded timeout/output, process-group cleanup, protected
  assistant paths denied). Worker claims and verified facts stay
  separate: results are reported as verified only after independent
  local checks; otherwise "verification was not available."
- **Sleep/wake with 60 s meaningful-idle timeout** — starts SLEEPING (no
  Live session, no mic forwarding). Waking opens one fresh session;
  sleeping closes it cleanly with no reconnect loop. Only accepted
  interactions reset the timer; background/media audio never does.
- **Media-aware auto-sleep** — MPRIS `Playing` edge (read-only
  `playerctl` poll) sleeps Chibi; stopping never wakes; F2 remains the
  wake mechanism; detection failure is harmless.
- **Single overlay window** — exactly one `RadialVisualizerWindow` per
  process, enforced by a singleton factory; hide/show reuses it.
- **Graceful shutdown** — session, audio, and Qt all unwind cleanly.

## High-level architecture

```
User
  │  voice (F2 to wake) · F1 UI · F3 quit
  ▼
Chibi / Gemini Live assistant (gemini-3.8-live)
  ├─ desktop + web + media + vision tools (31, approval-gated writes)
  ├─ Weather · Memory · read-aloud (deterministic local modules)
  ▼
specialized external coding worker (delegated, only when appropriate)
  ▼
independent verification (files checked, optional test command)
```

```
Hyprland F1/F2/F3
      │  (global hotkeys; user adds the three create_bind lines)
      ▼
bin/assistant-control ── Unix socket ($XDG_RUNTIME_DIR/cat-talker/control.sock)
      │  toggle = sleep/wake · ui-toggle/show/hide = visibility · stop = quit
      │  forwards to a live instance; otherwise launches exactly one
      │  (lock-guarded, lock fd inherited by the child, stale-socket
      │  handling, real failure reporting, no duplicates ever)
      ▼
cat_talker.main (Qt overlay + UiBridge: all widget ops on the main thread)
      │  callbacks via Qt signals        control thread via call_soon_threadsafe
      ▼                                  ▼
GeminiDesktopAgent ── Gemini Live session (connect only when awake)
  ├─ audio.py      mic in (16 kHz, 512-frame) / TTS out (24 kHz),
  │                pause-while-sleeping, single authoritative stream,
  │                output-level mic suppression
  ├─ echo_suppress.py  monitor-reference echo suppression, bounded staging
  ├─ vision.py     grim/mss capture + Hyprland coordinate mapping
  ├─ tools.py      public tool surface + voice approval flow
  ├─ web_search.py current web search (DuckDuckGo Lite, stdlib only)
  ├─ webpage.py    page fetch + readable extraction (stdlib only)
  ├─ weather.py    Open-Meteo geocode → forecast (stdlib only, no key)
  ├─ memory.py     explicit validated preferences (local file, no network)
  ├─ speech.py     standalone TTS boundary (local engine, replaceable)
  ├─ media_watcher.py  read-only MPRIS Playing-edge watcher
  ├─ coding_worker.py  delegated execution + verification boundary
  ├─ sleep.py      sleep/wake state machine + idle policy
  └─ control.py    local control-socket server (runtime-dir locking)
```

Three concepts stay separate: **visibility** (F1), **listening state**
(F2), **process lifetime** (F3).

## Installation / setup

Prerequisites (system packages): `grim`, `ydotool` + running `ydotoold`
(for clicking/typing), `playerctl`, `wpctl`, `brightnessctl`,
`wl-clipboard`/`xclip`, `espeak-ng` (optional, for `read_aloud`), a
microphone + speakers (PipeWire), Hyprland for the global shortcuts.
Python deps live in the repo venv (`.venv`).

Configuration: a Gemini API key via `GEMINI_API_KEY` env or
`~/.config/cat-talker/config.json` (`api_key`). Without a key the app
exits with an error dialog instead of starting broken.

## How to run it

Terminal (from the repo root):

```bash
cd /home/guru/ai-assistant
PYTHONPATH=src .venv/bin/python -m cat_talker.main
```

Normal daily control is via the launcher (works from any directory):

```bash
/home/guru/ai-assistant/bin/assistant-control toggle   # wake or sleep
/home/guru/ai-assistant/bin/assistant-control status
```

The app starts SLEEPING (visible, silent, no Gemini connection) and waits
for F2. `assistant-control stop` quits it gracefully. If a second copy is
ever started, it refuses to run headless beside the live instance.

## Global shortcuts

Add these lines to `~/.config/hypr/hyprland/keybinds.lua`, then
`hyprctl reload`:

```lua
create_bind("F1", hl.dsp.exec_cmd("/home/guru/ai-assistant/bin/assistant-control ui-toggle"))
create_bind("F2", hl.dsp.exec_cmd("/home/guru/ai-assistant/bin/assistant-control toggle"))
create_bind("F3", hl.dsp.exec_cmd("/home/guru/ai-assistant/bin/assistant-control stop"))
```

- **F1** = show/hide UI only. Never wakes, never sleeps, never reconnects.
- **F2** = sleep/wake toggle. Sleeping closes the Live session; waking
  opens a fresh one. Deferred (never interrupted) mid tool call/response.
  Voice "go to sleep" also sleeps; there is no voice-wake from sleep.
- **F3** = graceful quit (session closes, audio closes, Qt exits).

## Major security boundaries

- Spoken approval gates all writes/commands/coding delegation; repeats
  need re-approval after 120 s.
- Coding worker: explicit workspace only, argv-only bounded execution,
  protected assistant paths (`~/.config/cat-talker`, history) denied
  everywhere, outside-workspace report paths dropped — but there is **no
  OS-level sandbox**: a compromised same-UID worker process could
  theoretically escape. Verification is independent of worker claims.
- Fetched web content is UNTRUSTED DATA (summarize, never obey).
- Memory writes are explicit only; secrets are never special-cased into
  preferences.

## Known limitations

- Echo suppression/audio isolation is improved but defensive, not a
  perfect guarantee; live-mic validation across wake cycles is still
  real-world-pending.
- Generic visual clicking is best-effort (`ydotoold` + `input` group).
- No voice wake-word while sleeping (F2 required); Hyprland needed for
  global keys; needs network + a valid Gemini key.
- Coding worker has no OS sandbox (see above); worker-reported tests
  are claims until independently verified.
- `read_aloud` needs `espeak-ng`, else honest failure.
- Weather needs network (Open-Meteo); web search/fetch are bounded
  best-effort; English/Hindi responses only.

## How to run tests

```bash
./.venv/bin/python -m pytest -q
```

374 passed, no hardware or network needed (fakes for Live sessions,
audio, vision, Qt offscreen, playerctl, subprocess, network). Covers
session lifecycle, single mic-worker ownership, stream recreation,
computer-use grounding, echo DSP, sleep/wake + F1/F2/F3 control,
single-instance launch, window lifecycle, shutdown, voice config
(VAD/hints/guard), media watcher, web info, Weather, Memory, TTS,
coding worker + verification, diagnostics, and packaging.

Current state: checkpoint `5ee950c`, all green; next phase is product
acceptance and real-world validation (see `ROADMAP.md`). No `AGENTS.md`
exists in this repo; engineering rules live with the maintainer.
