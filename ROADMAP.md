Completed (all implemented and committed as of `ec7276f`, follows `5ee950c`)
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
└── Auto-hide avatar UI after successful desktop-opening actions
    (visibility-only; never sleeps)

Current phase: product acceptance, polish, real-world reliability
├── End-to-end acceptance testing (live mic across wake cycles)
├── Spoken acceptance: workspace 5/6 switching + auto-hide round-trip
└── (hide → F1 restore; workspace change must not hide)
├── Documentation maintenance
├── UX/polish based on real usage
├── Remaining audio/echo edge cases
├── Stronger computer-use grounding
└── Worker sandboxing improvements if needed

Later (genuinely future, not implemented)
├── Dedicated computer-use model
├── Rich worker reports
└── More advanced automation
