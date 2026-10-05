# AI Assistant — current verified project state

Checkpoint: **`ec7276f`** — *Improve desktop navigation and assistant UX*
(follows `5ee950c` *Stabilize voice lifecycle and complete core assistant
stack*). Working tree matches the checkpoint except untracked `.vscode/`
(IDE state, not project source); everything below is implemented,
unit-tested (409 passed via `./.venv/bin/python -m pytest -q`), and
committed. No hardware, network, speakers, or credentials needed for
the suite (fakes for Live sessions, audio, vision, Qt offscreen,
playerctl, subprocess, network).

- Sleep/wake implemented: startup SLEEPING, F2 toggle with busy
  deferral, 60 s meaningful-idle timeout, greeting-once, no reconnect
  while sleeping, mic capture paused while sleeping.
- F1/F2/F3 control model: `bin/assistant-control` over a Unix control
  socket (`$XDG_RUNTIME_DIR/cat-talker/control.sock`); F1 = visibility
  only, F2 = sleep/wake only, F3 = graceful quit. No Python hotkeys.
- Single-instance launch hardening: dial-first, lock-guarded single
  launch, stale-socket handling, plus exclusive runtime-dir lock held
  for the server's lifetime and a launcher that passes the held lock
  fd to its child — a second process can never run headless beside the
  first (previously caused duplicate microphone pipelines). No
  killall/pkill.
- Single mic-worker ownership: one capture worker per live Gemini
  session (START/EXIT accounting with per-generation ids, peak-concurrency
  tracking, pre-creation guard kills leftovers); old generations cannot
  feed audio (generation gate + stale-input flush).
- Serialized microphone stream recreation: watchdog recreates a dead
  input stream under lock exactly once (stop → close → open), with
  stream ids in the logs; no parallel input streams.
- Single-window invariant: one `RadialVisualizerWindow` per process via
  a singleton factory; hide/show/flag changes reuse it; control and
  agent paths never construct windows.
- Current architecture: Qt overlay (`main.py` + `UiBridge`) ->
  `GeminiDesktopAgent` (Gemini Live, model `gemini-3.8-live`) with
  `audio.py` (16 kHz mic / 24 kHz output), `echo_suppress.py`
  (monitor-reference suppression, bounded staging), `vision.py`
  (grim/mss + Hyprland coordinates), `tools.py` (31 tools incl. voice
  approval), `sleep.py`, `control.py`, `media_watcher.py`,
  `web_search.py`, `webpage.py`, `weather.py`, `memory.py`,
  `speech.py`, `coding_worker.py`.
- Voice path: 512-frame (32 ms) mic chunks shared by capture and echo
  DSP; explicit Live VAD (enabled; HIGH start sensitivity, LOW end
  sensitivity, 300 ms prefix padding, 700 ms end silence); input
  transcription hints `["en-IN", "hi-IN"]` (VERBATIM); transcript
  safety guard keeps short commands (`sleep`, `wake up`, `yes`, `no`,
  `stop`) actionable while filtering single-letter/punctuation/
  non-English-Hindi noise. System guidance expects mixed
  English/Hindi/Hinglish speech.
- Major completed capabilities: voice dialogue, on-demand vision,
  OS tools, deterministic local date/time, current web search + page
  fetch (read-only, bounded, untrusted-fenced), Weather (Open-Meteo,
  explicit place → geocode → forecast), explicit Memory/Preferences
  (5-key validated store on the existing local file, explicit writes
  only), deterministic `read_aloud` (local espeak-ng PCM into the
  existing playback path), external coding worker (delegated execution
  in an explicitly selected workspace + independent verification),
   sleep/wake + F1/F2/F3 control plane, media-aware auto-sleep
   (MPRIS `Playing` edge → sleep; never wakes, never pauses media),
   Hyprland workspace switching (user-facing workspaces 1–6; numeric
   input validated and clamped 1–10; success verified against the
   active workspace), auto-hide avatar UI after successful
   desktop-opening/focus actions (`open_application`, `open_website`,
   `open_file`, `focus_or_launch`, `search_and_play_youtube`;
   visibility-only via `UiBridge.hide()` — never sleeps, never pauses
   media; error results and all other tools never hide),
   graceful shutdown, conversation history log, JSON config.
- Local date/time: `get_current_datetime` is a pure read-only tool in
  `ALL_TOOLS` and is deliberately NOT in `SIDE_EFFECT_TOOLS`. Clock,
  web-search, and weather concepts stay separate in the guidance.
- Memory is explicit, never automatic: `get/set/delete_preference`
  only; reads are exempt from side-effect dedup, writes follow the
  `save_user_preference` precedent. Weather may use a stored
  `weather_location`; nothing is inferred from IP or hidden state.
- Coding worker (delegated, not a second assistant): `run_coding_task`
  needs spoken approval like other risky actions; argv-only bounded
  execution with process-group cleanup; workspace realpath containment
  with protected assistant paths denied. Worker claims and verified
  facts are strictly separated (`tests_passed` = worker claim;
  `verification` ∈ not_run / worker_reported_* /
  independently_verified_*): Chibi reports "Worker completed;
  verification was not available." unless independently verified, and
  never says "All tests passed" without verified basis.
- Known limitations:
  - **Echo suppression/audio isolation is improved but defensive, not
    a perfect guarantee** against loud external audio. Live-mic
    validation across wake cycles is still real-world-pending.
   - **Generic visual clicking is best-effort.** Grounding can miss;
     bounded by max-2-alternate retry, then the assistant asks the user.
     Requires `ydotoold` + `input` group. Backend health is explicit:
     `bin/assistant-control ydotool-status` reports
     healthy/missing/stopped/unusable/permission without performing
     input; `bin/assistant-ydotool-setup` enables the packaged user
     service (explicit, user-level, never sudo).
  - **No voice wake-word while sleeping (F2 required);** Hyprland needed
    for global keys; needs network + valid Gemini API key.
  - **The coding worker has NO OS-level sandbox.** The boundary is
    validation + cwd/`--dir` scoping + approval + verification; a
    compromised same-UID worker process could theoretically escape it.
   - **`read_aloud` needs `espeak-ng`** for standalone speech; without
     it, it fails honestly with install guidance.
   - Live voice-command end-to-end for workspace 5/6 switching and the
     auto-hide round-trip (hide → F1/`"Show yourself"` restore) is
     real-world-pending: verified at the `hyprctl`/tool level plus
     offscreen Qt tests, awaiting a spoken acceptance pass.
  - Weather needs network (Open-Meteo, no key); web search/fetch are
    bounded best-effort (single DuckDuckGo Lite provider; UNTRUSTED
    DATA discipline); English/Hindi responses only.
- Future work (acceptance phase, not architecture rewrites):
  end-to-end real-world acceptance testing, documentation upkeep,
  UX polish from real usage, remaining audio/echo edge cases, stronger
  visual grounding, worker sandboxing improvements if needed.
