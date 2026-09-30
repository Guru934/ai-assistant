"""
Cat Talker - A local, privacy-first desktop AI assistant with real-time voice,
vision, and OS control capabilities.
"""

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


def __getattr__(name):
    # Lazy `main`: importing it eagerly here would load cat_talker.main as a
    # side effect of `import cat_talker`, so `python -m cat_talker.main`
    # would then load the module twice (RuntimeWarning). `from cat_talker
    # import main` still works via this PEP 562 hook.
    if name == "main":
        from cat_talker.main import main
        # Cache the function on the package: importing the submodule sets
        # the parent's `main` attribute to the MODULE, which would otherwise
        # shadow this hook (and the original public API) on later lookups.
        globals()["main"] = main
        return main
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")