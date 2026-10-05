"""Regression tests for the voice-listening quality correction.

Covers: shared 512-frame chunk size, explicit VAD config, en-IN/hi-IN
transcription hints, transcript-guard preservation, and no-behavior-
change in TTS/Weather/Memory/worker tools. No microphone, speakers,
or Gemini credentials needed.
"""

import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cat_talker import audio as audio_mod
from cat_talker import echo_suppress as echo_mod
from cat_talker.agent import (
    build_live_config,
    build_system_instructions,
    is_actionable_transcript,
)
from cat_talker.tools import ALL_TOOLS


# 1-3: shared chunk size ───────────────────────────────────────────

def test_shared_chunk_size_is_512():
    assert echo_mod.CHUNK_FRAMES == 512
    assert echo_mod.BYTES_PER_CHUNK == 512 * 2
    assert echo_mod.PACE_FRAMES == echo_mod.CHUNK_FRAMES


def test_audio_input_uses_shared_chunk_size():
    assert audio_mod.CHUNK_FRAMES is echo_mod.CHUNK_FRAMES
    src = inspect.getsource(audio_mod.AudioInterface.__init__)
    assert "frames_per_buffer=CHUNK_FRAMES" in src
    # Output path untouched: still fixed 1024-frame 24 kHz playback.
    assert "rate=24000" in src


def test_chunk_math_matches_live_guidance():
    # 512 frames @ 16 kHz = 32 ms, inside 20-40 ms streaming guidance.
    assert echo_mod.CHUNK_FRAMES / echo_mod.RATE == 0.032


# 4-6: explicit VAD ────────────────────────────────────────────────

def _vad():
    config = build_live_config("instructions")
    assert config.realtime_input_config is not None
    vad = config.realtime_input_config.automatic_activity_detection
    assert vad is not None
    return vad


def test_vad_explicitly_enabled():
    assert _vad().disabled is False


def test_vad_prefix_padding_present():
    assert _vad().prefix_padding_ms == 300


def test_vad_silence_duration_present():
    assert _vad().silence_duration_ms == 700


def test_vad_sensitivities_are_sdk_enums():
    from google.genai import types
    assert _vad().start_of_speech_sensitivity == \
        types.StartSensitivity.START_SENSITIVITY_HIGH
    assert _vad().end_of_speech_sensitivity == \
        types.EndSensitivity.END_SENSITIVITY_LOW


def test_live_config_keeps_audio_modality_and_tools():
    from google.genai import types
    config = build_live_config("instructions")
    mods = config.response_modalities
    assert mods is not None
    assert types.Modality.AUDIO in mods
    assert types.Modality.TEXT in mods
    # Audio path untouched: audio still streams for playback.
    assert len(mods) == 2
    # The SDK copies the tool list; elements are the same callables.
    assert config.tools is not None
    assert list(config.tools) == list(ALL_TOOLS)


# 7: language hints ────────────────────────────────────────────────

def test_transcription_hints_en_in_and_hi_in():
    config = build_live_config("instructions")
    trans = config.input_audio_transcription
    assert trans is not None
    codes = trans.language_codes or []
    assert list(codes) == ["en-IN", "hi-IN"]
    # Mode unset: SDK default VERBATIM transcription is preserved.
    assert trans.mode is None


def test_guidance_expects_mixed_speech():
    text = build_system_instructions()
    assert "Hinglish" in text
    assert "mixing" in text


# 8: guard preserves short commands ────────────────────────────────

def test_guard_keeps_short_commands_actionable():
    for cmd in ("stop", "sleep", "wake up", "yes", "no",
                "STOP", "Yes", " go to sleep "):
        assert is_actionable_transcript(cmd) is True, cmd


def test_guard_still_filters_garbage():
    for junk in ("", "   ", "X", "...", "好啊。", None, 42):
        assert is_actionable_transcript(junk) is False, repr(junk)


# 10: unrelated behavior unchanged ─────────────────────────────────

def test_unrelated_tool_surfaces_unchanged():
    names = [f.__name__ for f in ALL_TOOLS]
    for tool in ("get_weather", "get_preference", "set_preference",
                 "delete_preference", "read_aloud", "run_coding_task",
                 "get_current_datetime", "web_search"):
        assert names.count(tool) == 1, tool
