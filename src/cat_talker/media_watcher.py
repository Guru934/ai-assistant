"""Read-only MPRIS media watcher (playerctl, argv-only, best-effort).

Purpose: when external media (e.g. Brave/YouTube) starts playing, Chibi
should automatically sleep so background audio is never mistaken for
user speech. Detection uses MPRIS `playerctl status` only - never raw
speaker-audio levels, never title parsing: any player reporting
"Playing" counts as playback.

Rules enforced by the caller (agent.run_loop), not here:
- edge-triggered: only a stopped->playing transition sleeps;
- media stopping never wakes (F2 remains the wake mechanism);
- query failure is harmless and never affects sleep/wake;
- exactly one watcher task exists per run_loop (created beside the
  idle watchdog, cancelled with it).
"""

import shutil
import subprocess

from cat_talker.logging_config import get_logger

logger = get_logger("cat_talker.media_watcher")

QUERY_TIMEOUT_S = 2.0


def query_media_playing():
    """True if any MPRIS player reports Playing, False if none does,
    None when the state is unknown (no playerctl, error, bad output).

    Never raises.
    """
    if not shutil.which("playerctl"):
        return None
    try:
        res = subprocess.run(
            ["playerctl", "status"],
            capture_output=True, text=True, timeout=QUERY_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    except Exception:
        return None
    if res.returncode != 0:
        return None
    try:
        lines = (res.stdout or "").splitlines()
    except Exception:
        return None
    if not lines:
        return False
    # Default `playerctl status` prints one bare status per line
    # ("Playing"); custom formats may embed it as a `|`-separated
    # segment. Either way only the status token is read, never titles.
    for line in lines:
        cells = [c.strip().lower() for c in line.split("|")]
        if any(cell == "playing" for cell in cells):
            return True
    return False


class MediaWatcher:
    """Edge-triggered playback -> sleep. One instance per run_loop."""

    def __init__(self):
        self._was_playing = False

    def poll_once(self, agent) -> bool:
        """Poll once. Returns True iff this poll caused a sleep request.

        Never raises; never wakes; never pauses or resumes media.
        """
        try:
            playing = query_media_playing()
        except Exception:
            return False
        if playing is None:
            return False
        was = self._was_playing
        self._was_playing = bool(playing)
        if playing and not was:
            logger.info("Media playback started: auto-sleeping")
            try:
                agent.request_sleep()
            except Exception as e:
                logger.error(f"Media auto-sleep failed: {e}")
            return True
        return False
