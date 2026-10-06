Completed (all implemented and committed as of `f4514ae`, follows `ddcf750`, `47039dd`, `dfac8f0`, `dc95c62`, `ae92f80`, `b72d466`, `64ad9d9`, `9e88759`, `69b3b63`, `5cfb01b`, `9058e57`, `ec7276f`, `5ee950c`)
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
└── Local reminders, first slice (create/list/cancel; once-exact +
    daily-recurring; validated JSON store; deterministic scheduler;
    notification delivery only; sleep/wake-independent; clean
    shutdown; persistent cancellation; explicit-request model
    contract)

Current phase: product acceptance, polish, real-world reliability
├── End-to-end acceptance testing (live mic across wake cycles)
├── Spoken acceptance: workspace 5/6 switching + auto-hide round-trip
└── (hide → F1 restore; workspace change must not hide)
├── Documentation maintenance
├── UX/polish based on real usage
├── Remaining audio/echo edge cases
└── Worker sandboxing improvements if needed

Later (genuinely future, not implemented)
├── Dedicated computer-use model
└── More advanced automation
