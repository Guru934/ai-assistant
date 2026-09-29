"""
Cat Talker - A local, privacy-first desktop AI assistant with real-time voice,
vision, and OS control capabilities.
"""

from cat_talker.main import main
from cat_talker.agent import start_agent_in_thread, GeminiDesktopAgent
from cat_talker.audio import AudioInterface
from cat_talker.vision import VisionInterface
from cat_talker.tools import ALL_TOOLS

__version__ = "0.1.0"
__all__ = [
    "main",
    "start_agent_in_thread",
    "GeminiDesktopAgent",
    "AudioInterface",
    "VisionInterface",
    "ALL_TOOLS",
]