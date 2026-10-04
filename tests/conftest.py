"""Suite-wide determinism: the read-only MPRIS media watcher is OFF for
unit tests (CAT_TALKER_MEDIA_WATCH=0), so a player reporting Playing on
the test machine can never auto-sleep a driven run_loop. Tests that
exercise the watcher itself opt back in per-test via monkeypatch."""

import os

os.environ.setdefault("CAT_TALKER_MEDIA_WATCH", "0")
