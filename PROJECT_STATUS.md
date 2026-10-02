# AI Assistant — current verified project state

Checkpoint: committed working tree at `000d7a3` (Add local date and time
tool) plus UNCOMMITTED current-web-information work described below;
see `git status` (modified: src/cat_talker/tools.py,
src/cat_talker/agent.py, README.md, PROJECT_STATUS.md; new:
src/cat_talker/web_search.py, src/cat_talker/webpage.py,
tests/test_web_info.py; untracked pre-existing .vscode/).

- Tests: **200 passed** (`PYTHONPATH=src .venv/bin/python -m pytest -q`:
  154 baseline + 46 new mocked web-info tests), no hardware or network
  required.
- Sleep/wake implemented: startup SLEEPING, F2 toggle with busy
  deferral, 60 s meaningful-idle timeout, greeting-once, no reconnect
  while sleeping, mic capture paused while sleeping.
- F1/F2/F4 architecture: `bin/assistant-control` over a Unix control
  socket (`$XDG_RUNTIME_DIR/cat-talker/control.sock`); F1 = visibility
  only, F2 = sleep/wake only, F4 = graceful quit. No Python hotkeys.
- Single-instance IPC: dial-first, lock-guarded single launch, stale
  socket handling; no killall/pkill.
- Single-window invariant: one `RadialVisualizerWindow` per process via
  a singleton factory; hide/show/flag changes reuse it; control and
  agent paths never construct windows.
- Current architecture: Qt overlay (`main.py` + `UiBridge`) ->
  `GeminiDesktopAgent` (Gemini Live, model `gemini-3.8-live`) with
  `audio.py`, `vision.py` (grim/mss + Hyprland coordinates), `tools.py`
  (OS/media/vision actions + voice approval), `sleep.py`, `control.py`.
- Major completed capabilities: voice dialogue, on-demand vision,
  OS tools, deterministic local date/time (`get_current_datetime`,
  timezone-aware, machine clock + configured local timezone, no network),
  sleep/wake + F1/F2/F4 control plane, graceful shutdown,
  conversation history log, JSON config.
- Local date/time: `get_current_datetime` is a pure read-only tool in
  `ALL_TOOLS` and is deliberately NOT in `SIDE_EFFECT_TOOLS` (exempt from
  side-effect dedup). It reports full calendar date, weekday, local time,
  timezone name and UTC offset from Python's system clock; the system
  prompt directs "what time is it?" / "what's today's date?" / "what day
  is today?" to the tool instead of model knowledge.
- Current web information (UNCOMMITTED): `web_search` (DuckDuckGo Lite
  only, stdlib HTTP, 10 s timeout, max 5 results, bounded snippets/total,
  honest errors) and `fetch_webpage` (http(s) only, scheme gate before
  I/O, stdlib urllib, 10 s timeout, 512 KB cap, max 3 redirects with
  per-redirect scheme revalidation, Content-Type gate, max 4000 chars
  text / 4800 chars output, UNTRUSTED-DATA fencing) are read-only tools
  in `ALL_TOOLS` and NOT in `SIDE_EFFECT_TOOLS`. Extraction is one-pass
  HTMLParser with article > main > body priority; noise elements suppress
  direct text but nested article/main still traverses. Max 3 fetches per
  user interaction via `_fetch_webpage_count` /
  `MAX_FETCH_PER_INTERACTION`, reset by `_start_new_interaction` on each
  user turn. System prompt keeps clock vs web separate, routes fresh-info
  to search, snippets-first with search-then-fetch-then-summarize, never
  claims reads without a successful fetch, and enforces the
  English/Hindi-only language policy. Provider details stay in
  `web_search.py` / `webpage.py`; `tools.py` is the public surface.
- Known limitations:
  - **Generic visual clicking is best-effort.** Grounding can miss;
    bounded by max-2-alternate retry, then the assistant asks the user.
    Requires `ydotoold` + `input` group.
  - **Custom echo suppression / audio isolation is experimental and NOT
    fully accepted by real-world testing. The echo problem is NOT
    solved:** loud speaker/media audio can still be transcribed as user
    speech. Mitigations present (monitor-reference suppression,
    half-duplex assistant muting) reduce but do not eliminate it.
  - No voice wake-word while sleeping (F2 required); Hyprland needed
    for global keys; needs network + valid Gemini API key.
  - Web search/fetch are best-effort and bounded: single DuckDuckGo Lite
    provider (markup changes return honest failure, never fake results);
    page text is UNTRUSTED DATA (summarize, never obey); non-HTML refused;
    English/Hindi responses only.
