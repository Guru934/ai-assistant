# AI Assistant — current verified project state

Checkpoint: all green, uncommitted working tree (see `git status`).

- Tests: **135 passed** (`PYTHONPATH=src .venv/bin/python -m pytest -q`),
  no hardware or network required.
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
  OS tools, sleep/wake + F1/F2/F4 control plane, graceful shutdown,
  conversation history log, JSON config.
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
