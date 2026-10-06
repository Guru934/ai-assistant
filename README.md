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
- **OS control via tools** — 34 tools: open apps/sites/files, clipboard,
  volume, brightness, workspaces, media (`playerctl`), notifications,
  reminders, YouTube search-and-play, and preferences. Risky actions
  (`click_screen`, `type_text`, `press_key`, `run_coding_task`) require
  spoken "yes" approval first. Clicks are visually grounded: every
  inspected frame carries a `frame_seq` number, `click_screen` must be
  given the current frame's `frame_seq`, and clicks grounded on older
  frames are refused as stale (max 2 alternate retries, then the
  assistant asks the user). Multi-step computer-use runs keep a
  `ComputerUseContext` (geometry + frame/action sequences) across
  inspect/click/verify turns instead of re-deriving state each call.
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
  OpenCode is the default backend; Goose is an explicit opt-in
  backend behind the same provider abstraction. `goose` uses
  `gemini-3.8-flash` by default; `goose-lite` uses
  `gemini-3.5-flash-lite`. Both use Chibi's secure Gemini key handed
  to the Goose child only as `GOOGLE_API_KEY`. Either Goose variant
  can be given an explicit model through `CAT_TALKER_GOOSE_MODEL`.
  Neither backend is sandboxed.
- **Sleep/wake with 60 s meaningful-idle timeout** — starts SLEEPING (no
  Live session, no mic forwarding). Waking opens one fresh session;
  sleeping closes it cleanly with no reconnect loop. Only accepted
  interactions reset the timer; background/media audio never does.
- **Media-aware auto-sleep** — MPRIS `Playing` edge (read-only
  `playerctl` poll) sleeps Chibi; stopping never wakes; F2 remains the
  wake mechanism; detection failure is harmless.
- **Single overlay window** — exactly one `RadialVisualizerWindow` per
  process, enforced by a singleton factory; hide/show reuses it.
- **Local reminders** — `create/list/cancel_reminder` for explicit
  "remind me ..." requests only (never inferred): one-time exact
  timezone-aware datetimes and daily HH:MM times, persisted as
  validated JSON (`~/.config/cat-talker/reminders.json`, atomic
  writes), driven by a single deterministic daemon scheduler thread
  that fires through the existing desktop-notification path. Once
  reminders disable after firing; daily ones advance; cancellation
  persists across restarts. The scheduler is independent of the Live
  sleep/wake state and stops cleanly on quit. Model contract: only
  once/daily schedules, local-timezone interpretation, read-only
  listing, cancel existing ids only, ask rather than guess when the
  time is ambiguous. Reminders fire only while the process runs; no
  snooze/edit, no weekly/custom recurrence.
- **Readable speech bubble** — caption text wraps inside a full-width
  top zone (never clipped); the avatar shifts down and shrinks while
  the bubble shows so the two never overlap.
- **System tray** — tray icon with live status row (sleeping/awake +
  overlay visibility, refreshed on every menu open), Show window,
  Wake, Sleep, Settings, a read-only ydotool status dialog, and Quit.
- **Settings UI** — in-app panel (tray Settings or spoken request) for
  API key (saved to the OS keyring), monitor, voice, and approvals.
- **Secure API-key storage** — key lives in the SecretService keyring
  via `keyring`, never plaintext; a legacy `api_key` in
  `~/.config/cat-talker/config.json` is auto-migrated on startup and
  the file copy scrubbed. Precedence: secure store, then legacy
  (migrating), then `GEMINI_API_KEY`.
- **Optional local wake word** — "Hey Jarvis" via openwakeword, off by
  default, fully offline (no audio leaves the PC, no Gemini session
  while sleeping); wakes through the same path as F2.
- **Assistant text stream** — TEXT is assembled from
  output-audio-transcription events alongside AUDIO (the API rejects an
  explicit AUDIO+TEXT modality pair for this model), so replies are
  available as text without changing the audio path.
- **Graceful shutdown** — session, audio, and Qt all unwind cleanly.

## High-level architecture

```
User
  │  voice (F2 to wake) · F1 UI · F3 quit
  ▼
Chibi / Gemini Live assistant (gemini-3.8-live)
  ├─ desktop + web + media + vision tools (34, approval-gated writes)
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
  ├─ reminders.py    validated reminder store + deterministic scheduler
  ├─ ydotool_health.py read-only ydotool backend health + setup hints
  ├─ tray.py         system-tray icon/menu (live status, ydotool dialog)
  ├─ settings.py     in-app settings panel (key, monitor, voice)
  ├─ credentials.py  SecretService keyring API-key storage + migration
  ├─ wakeword.py     optional offline openwakeword detection
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

Input backend: computer-use injection uses the packaged `ydotool.service`
user unit (`/usr/bin/ydotoold`, persists across logins while enabled with
systemd lingering). Check it with `bin/assistant-control ydotool-status`;
enable/start it explicitly with `bin/assistant-ydotool-setup` (user level
only, never sudo). One-time host step for reboot-proof startup: `sudo
usermod -aG input $USER`, then log out/in once (the user unit otherwise
races logind's uaccess grant at boot and systemd gives up for the
boot). Click/type/key failures name the exact cause
(ydotool missing, daemon stopped, permission/uinput problem).

Configuration: a Gemini API key via `GEMINI_API_KEY` env, the Settings
panel, or `bin/assistant-control` setup — it is stored in the OS
SecretService keyring, never plaintext. A legacy `api_key` in
`~/.config/cat-talker/config.json` is migrated automatically and
scrubbed. Without any key the app exits with an error dialog instead
of starting broken.

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

Coding worker backend selection is explicit. OpenCode remains the default.
For Goose with Gemini 3.8 Flash:

```bash
export CAT_TALKER_CODER_BACKEND=goose
```

For Goose with Gemini 3.5 Flash-Lite:

```bash
export CAT_TALKER_CODER_BACKEND=goose-lite
```

For either Goose backend, `CAT_TALKER_GOOSE_MODEL` overrides the
backend's default model.

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
  Voice "go to sleep" also sleeps. Optional local wake-word ("Hey Jarvis",
  off by default) can also wake: install with
  `.venv/bin/python -m pip install openwakeword`, then set
  `wake_word_enabled: true` in `~/.config/cat-talker/config.json`.
  Detection is fully offline (no audio leaves the PC, no Gemini session
  while sleeping) and wakes through the same path as F2. A custom
  "Hey Chibi" model needs a trained `.onnx` (set `wake_word_model` to
  its path); the built-in `hey_jarvis` model is the validated default.
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
- Generic visual clicking is best-effort (`ydotoold` + `input` group,
  frame-grounded with stale refusal).
- The optional wake word needs `openwakeword` installed and
  `wake_word_enabled: true`; otherwise F2 wakes. Hyprland needed for
  global keys; needs network + a valid Gemini key.
- Coding worker has no OS sandbox (see above); worker-reported tests
  are claims until independently verified.
- `read_aloud` needs `espeak-ng`, else honest failure.
- Reminders fire only while the Chibi process runs (no daemon
  persistence beyond the session); once/daily only, no snooze/edit,
  notification delivery only — never scheduled clicks, keys, shell,
  coding, or web actions. Live F2 wake/listen and F3 quit have since
  been human-verified PASS on the real desktop; reminder firing itself
  was validated through the real notification daemon/production path
  (headless visual caveat stands).
- Weather needs network (Open-Meteo); web search/fetch are bounded
  best-effort; English/Hindi responses only.

## How to run tests

```bash
./.venv/bin/python -m pytest -q
```

600 passed, no hardware or network needed (fakes for Live sessions,
audio, vision, Qt offscreen, playerctl, subprocess, network). Covers
session lifecycle, single mic-worker ownership, stream recreation,
computer-use grounding + stale-frame protection + multi-step context,
echo DSP, sleep/wake + F1/F2/F3 control, single-instance launch,
window lifecycle, shutdown, voice config (VAD/hints/guard), wake
word, tray + bubble UI, settings, secure credentials + migration,
reminders (store, scheduler, contract), Goose opt-in backend +
credential handoff, media watcher, web info, Weather, Memory, TTS, assistant text
stream, coding worker + verification, workspace switching
(user-facing 1–6), auto-hide visibility behavior, diagnostics, and
packaging.

Current state: automated suite 600 passed at the `9e32be0` code
checkpoint (current GitHub main); live desktop acceptance
human-verified PASS (workspace 5/6, app-open auto-hide, F1 restore,
F2 wake/listen, F3 quit; no defects) — recorded separately from the
automated suite. Next phase is product acceptance and real-world
validation (see `ROADMAP.md`). No `AGENTS.md`
exists in this repo; engineering rules live with the maintainer.
