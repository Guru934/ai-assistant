"""Suite-wide determinism: the read-only MPRIS media watcher is OFF for
unit tests (CAT_TALKER_MEDIA_WATCH=0), so a player reporting Playing on
the test machine can never auto-sleep a driven run_loop. Tests that
exercise the watcher itself opt back in per-test via monkeypatch.

Same rule for the local wake-word detector (CAT_TALKER_WAKEWORD=0): no
test spawns real detector threads or loads the onnx model unless it
explicitly opts in (test_wakeword.py does, with fakes)."""

import os

os.environ.setdefault("CAT_TALKER_MEDIA_WATCH", "0")
os.environ.setdefault("CAT_TALKER_WAKEWORD", "0")
