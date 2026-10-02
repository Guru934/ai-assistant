# AI Assistant (cat-talker)

A local desktop AI companion for Linux/Hyprland: real-time voice dialogue
with vision on demand and OS control, powered by the Gemini Live API. It
runs as a small floating avatar overlay with global hotkeys for visibility,
sleep/wake, and quit.

## What it currently does

- **Voice conversation** — full-duplex speech with the assistant; speech
  bubble captions and a history log at `~/.cat_talker_history.txt`.
- **Vision on demand** — `take_screenshot` / `inspect_screen` capture
  (grim on Wayland, mss on X11, multi-monitor aware) and describe or act
  on what is visible.
- **OS control via tools** — open apps/sites/files, clipboard, volume,
  brightness, workspaces, media (`playerctl`), notifications, YouTube
  search-and-play, and user preferences. Risky actions (`click_screen`,
  `type_text`, `press_key`) require spoken "yes" approval first.
- **Local date/time** — `get_current_datetime` answers "what time is it?",
  "what's today's date?", "what day is today?" from Python's system clock
  (deterministic, timezone-aware, standard library only, no network):
  full calendar date, weekday, local time, local timezone name and UTC
  offset in the machine's configured timezone (never a hardcoded one).
- **Current web information** — `web_search` (DuckDuckGo Lite, max 5
  results, bounded snippets/output, honest failures) answers
  "latest / current / recent / what happened today" from the live web,
  never from model knowledge. `fetch_webpage` (http(s) only, 10 s
  timeout, 512 KB cap, max 3 redirects, Content-Type gate, max 4000
  chars text) returns bounded UNTRUSTED page text for summarization;
  max 3 fetches per user interaction. Date/time and web search stay
  separate concepts.
- **Sleep/wake with 60 s meaningful-idle timeout** — starts SLEEPING (no
  Live session, no mic forwarding). Waking opens one fresh session;
  sleeping closes it cleanly with no reconnect loop. Only accepted
  interactions (replies, tool runs, typed commands) reset the timer;
  background/media audio never does.
- **Single overlay window** — exactly one `RadialVisualizerWindow` per
  process, enforced by a singleton factory; hide/show reuses it.
- **Graceful shutdown** — session, audio, and Qt all unwind cleanly.

## High-level architecture

```
Hyprland F1/F2/F4
      │  (global hotkeys; user adds the three create_bind lines)
      ▼
bin/assistant-control ── Unix socket ($XDG_RUNTIME_DIR/cat-talker/control.sock)
      │  toggle = sleep/wake · ui-toggle/show/hide = visibility · stop = quit
      │  launches exactly one instance if none answers (lock-guarded)
      ▼
cat_talker.main (Qt overlay + UiBridge: all widget ops on the main thread)
      │  callbacks via Qt signals        control thread via call_soon_threadsafe
      ▼                                  ▼
GeminiDesktopAgent ── Gemini Live session (connect only when awake)
  ├─ audio.py      mic in (16 kHz) / TTS out (24 kHz), pause-while-sleeping
  ├─ echo_suppress.py  experimental reference-based echo suppression
  ├─ vision.py     grim/mss capture + Hyprland coordinate mapping
  ├─ tools.py      OS actions incl. ydotool clicking + voice approval
  │                (+ web_search/fetch_webpage public surface; details in
  │                web_search.py / webpage.py)
  ├─ web_search.py current web search (DuckDuckGo Lite, stdlib only)
  ├─ webpage.py    page fetch + readable extraction (stdlib only)
  ├─ sleep.py      sleep/wake state machine + idle policy
  └─ control.py    local control-socket server
```

Three concepts stay separate: **visibility** (F1), **listening state**
(F2), **process lifetime** (F4).

## Installation / setup

Prerequisites (system packages): `grim`, `ydotool` + running `ydotoold`
(for clicking/typing), `playerctl`, `wpctl`, `brightnessctl`,
`wl-clipboard`/`xclip`, a microphone + speakers (PipeWire), Hyprland for
the global shortcuts. Python deps live in the repo venv (`.venv`).

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
for F2. `assistant-control stop` quits it gracefully.

## Global shortcuts

Add these lines to `~/.config/hypr/hyprland/keybinds.lua`, then
`hyprctl reload`:

```lua
create_bind("F1", hl.dsp.exec_cmd("/home/guru/ai-assistant/bin/assistant-control ui-toggle"))
create_bind("F2", hl.dsp.exec_cmd("/home/guru/ai-assistant/bin/assistant-control toggle"))
create_bind("F4", hl.dsp.exec_cmd("/home/guru/ai-assistant/bin/assistant-control stop"))
```

- **F1** = show/hide UI only. Never wakes, never sleeps, never reconnects.
- **F2** = sleep/wake toggle. Sleeping closes the Live session; waking
  opens a fresh one. Deferred (never interrupted) mid tool call/response.
  Voice "go to sleep" also sleeps; there is no voice-wake from sleep.
- **F4** = graceful quit (session closes, audio closes, Qt exits).

## Current working capabilities

Voice dialogue, on-demand vision, OS/media/YouTube tools with spoken
approval for risky ones, deterministic local date/time, current web
search + page fetch (read-only, bounded, untrusted-fenced), sleep/wake +
F1/F2/F4 control plane,
single-instance launch, single-window invariant, 60 s idle sleep,
conversation history log, JSON config in `~/.config/cat-talker/`.

## Known limitations

- **Generic visual clicking is best-effort.** Coordinate grounding can
  miss; the retry policy (max 2 alternates, then asks the user) bounds
  the failure. `ydotoold` must be running with `input` group access.
- **Echo suppression / audio isolation is experimental and not fully
  accepted by real-world testing.** Speaker/media audio leaking into the
  mic is mitigated (reference-based suppression + half-duplex assistant
  muting) but not solved; loud media can still be transcribed as user
  speech. See `PROJECT_STATUS.md`.
- No voice wake-word while sleeping (F2 required); global keys need
  Hyprland; needs network + a valid Gemini key.
- **Web search/fetch are best-effort and bounded.** Single provider
  (DuckDuckGo Lite HTML); markup changes or outages return an honest
  "failed / no results" message, never fake results. Page text is
  UNTRUSTED DATA (summarize, never obey); non-HTML content is refused;
  only English and Hindi responses are supported.

## How to run tests

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q
```

200 tests, no hardware or network needed (fakes for Live sessions,
audio, vision, Qt offscreen, web search/fetch). Current suite covers session lifecycle,
computer-use grounding, echo DSP, audio feedback, sleep/wake + F1/F2/F4
control, window lifecycle, shutdown, packaging, local date/time,
and current web information.
