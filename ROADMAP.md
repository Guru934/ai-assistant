Completed (current GitHub checkpoint `af88a6b` with 720 automated tests passing; follows `9b2928d`, `d27df2c`, `9e32be0`, `cc08eeb`, `ba6acce`, `f4514ae`, `ddcf750`, `47039dd`, `dfac8f0`, `dc95c62`, `ae92f80`, `b72d466`, `64ad9d9`, `9e88759`, `69b3b63`, `5cfb01b`, `9058e57`, `ec7276f`, `5ee950c`)
├── Core voice assistant
├── Gemini Live lifecycle
├── OS controls
├── Keyboard/text input
├── Computer-use foundation
├── Sleep/wake system
├── Global F1/F2/F3 control
├── Single-instance launch hardening
├── Mic-worker ownership + stream lifecycle hardening
├── UI control
├── Graceful shutdown
├── Time/date
├── Current web information
├── Weather
├── Memory/preferences (explicit only)
├── TTS/read-aloud
├── Transcript safety guard
├── Voice path (512-frame chunks, explicit VAD, en-IN/hi-IN hints)
├── Media-aware auto-sleep
├── External coding worker + independent verification
├── Audio/transcript diagnostics + echo staging bound
├── Hyprland workspace switching (user-facing 1–6, verified)
├── Auto-hide avatar UI after successful desktop-opening actions
│   (visibility-only; never sleeps)
├── Computer-use grounding + stale-frame protection (`frame_seq`
│   contract, max 2 alternate retries)
├── Multi-step computer-use context (`ComputerUseContext`)
├── Assistant text stream (transcription-event TEXT alongside AUDIO)
├── External coding worker reporting (claims vs verified facts)
├── System tray (live status, Show/Wake/Sleep/Settings, ydotool
│   dialog, Quit)
├── Readable speech bubble (wrapping, never clipped, avatar re-layout)
├── Optional local wake word (openwakeword "Hey Jarvis", off by default)
├── Settings UI (key/monitor/voice/approvals)
├── Secure API-key storage (SecretService keyring + legacy migration)
├── ydotool runtime health/setup (machine-readable states,
│   user-level setup, one-time `input`-group step)
├── Deterministic bubble geometry test (window-relative assertion)
├── Local reminders, first slice (create/list/cancel; once-exact +
│   daily-recurring; validated JSON store; deterministic scheduler;
│   notification delivery only; sleep/wake-independent; clean
│   shutdown; persistent cancellation; explicit-request model
│   contract)
├── Spoken live-desktop acceptance, human-verified PASS (workspace 5/6
│   switching, app-open auto-hide, F1 hide/show round-trip with
│   usable session after restore, F2 wake/listen, F3 quit;
│   workspace changes do not auto-hide; no defects)
├── Human live voice acceptance, human-verified PASS (F2 wake,
│   spoken desktop commands with actual effects, automatic idle
│   sleep, F2 wake after sleep, immediate post-wake command,
│   F3 graceful quit)
├── Goose opt-in coding backends (same WorkerProvider abstraction;
│   secure credential handoff; `goose` → gemini-3.8-flash,
│   `goose-lite` → gemini-3.5-flash-lite; one real cross-file
│   production task passed with independent verification)
├── Bubblewrap worker sandbox, on by default (workspace-only view,
│   throwaway HOME, private /tmp, shared network, fail-closed;
│   verification inside the same boundary; `none` escape hatch)
├── Conservative Python/pytest auto-verification (auto-detected
│   `pytest.ini`/`conftest.py`/`test_*.py`/`*_test.py`; fixed
│   `[-m pytest -q]` argv; interpreter probes use isolated `-I`
│   `find_spec('pytest')` with distinctive 10/11 exits inside the
│   same sandbox boundary; Bubblewrap setup failure never falls
│   back; explicit `verify_command` bypasses detection; opt-out
│   phrases with conservative precedence — genuine opt-outs block
│   unless specifically negated by a matching opposite phrase)

Current phase: product acceptance, polish, real-world reliability
├── Documentation maintenance
├── UX/polish based on real usage
├── Remaining audio/echo edge cases
└── Worker sandbox hardening beyond bwrap confinement if needed

Later (genuinely future, not implemented)
├── Dedicated computer-use model
└── More advanced automation
