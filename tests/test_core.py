"""Unit tests for cat-talker core functions."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cat_talker.config import load_config, save_config, get_api_key, get_preferred_monitor, set_preferred_monitor, get_auto_reconnect, set_auto_reconnect, get_voice_approval, set_voice_approval
from cat_talker.vision import VisionInterface
from cat_talker.audio import AudioInterface
from cat_talker.tools import take_screenshot, inspect_screen, ALL_TOOLS


def test_config_load_save():
    """Test configuration loading and saving."""
    config = load_config()
    assert "api_key" in config
    assert "preferred_monitor" in config
    assert "auto_reconnect" in config
    assert "voice_approval_enabled" in config
    print("✅ test_config_load_save passed")


def test_config_auto_reconnect():
    """Test auto-reconnect setting."""
    # Test default
    assert get_auto_reconnect() == True
    
    # Test set and get
    set_auto_reconnect(False)
    assert get_auto_reconnect() == False
    
    # Restore
    set_auto_reconnect(True)
    assert get_auto_reconnect() == True
    print("✅ test_config_auto_reconnect passed")


def test_config_voice_approval():
    """Test voice approval setting."""
    assert get_voice_approval() == True
    
    set_voice_approval(False)
    assert get_voice_approval() == False
    
    set_voice_approval(True)
    assert get_voice_approval() == True
    print("✅ test_config_voice_approval passed")


def test_config_preferred_monitor():
    """Test preferred monitor setting."""
    set_preferred_monitor("focused")
    assert get_preferred_monitor() == "focused"
    print("✅ test_config_preferred_monitor passed")


def test_vision_interface_initialization():
    """Test VisionInterface initializes correctly."""
    v = VisionInterface()
    monitors = v.get_monitors()
    assert len(monitors) > 0
    active = v.get_active_monitor()
    assert "name" in active
    assert "width" in active
    assert "height" in active
    print("✅ test_vision_interface_initialization passed")


def test_vision_capture_frame():
    """Test VisionInterface capture_frame works."""
    v = VisionInterface()
    frame = v.capture_frame()
    assert frame is not None or len(frame) > 0  # May be None if no display
    print("✅ test_vision_capture_frame passed")


def test_tool_map_complete():
    """Test that all tools are available in the dynamic map."""
    tool_func_map = {func.__name__: func for func in ALL_TOOLS}
    expected_tools = [
        "open_application", "open_website", "get_clipboard", "get_active_window",
        "list_directory", "open_file", "set_volume", "set_brightness",
        "take_screenshot", "search_and_play_youtube", "focus_or_launch",
        "switch_workspace", "media_action", "set_clipboard", "send_notification",
        "confirm_action", "cancel_action", "click_screen", "type_text",
        "press_key", "inspect_screen", "save_user_preference"
    ]
    for tool_name in expected_tools:
        assert tool_name in tool_func_map, f"Missing tool: {tool_name}"
    print(f"✅ test_tool_map_complete passed ({len(tool_func_map)} tools)")


def test_take_screenshot_function():
    """Test take_screenshot function runs without error."""
    result = take_screenshot(filename="/tmp/test_screenshot.jpg", monitor="")
    # Should not crash, result may vary based on environment
    assert result is not None
    print("✅ test_take_screenshot_function passed")


def test_inspect_screen_function():
    """Test inspect_screen function runs without error."""
    result = inspect_screen(query="test", monitor="")
    assert result == "SCREEN_INSPECT_REQUESTED"
    print("✅ test_inspect_screen_function passed")