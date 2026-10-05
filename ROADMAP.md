Completed (all implemented and committed as of `5ee950c`)
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
└── Audio/transcript diagnostics + echo staging bound

Current phase: product acceptance, polish, real-world reliability
├── End-to-end acceptance testing (live mic across wake cycles)
├── Documentation maintenance
├── UX/polish based on real usage
├── Remaining audio/echo edge cases
├── Stronger computer-use grounding
└── Worker sandboxing improvements if needed

Later (genuinely future, not implemented)
├── Dedicated computer-use model
├── Rich worker reports
└── More advanced automation
