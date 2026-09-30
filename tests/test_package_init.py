"""Focused test: importing the package must not eagerly load cat_talker.main.

Eager loading made `python -m cat_talker.main` execute the module twice
(RuntimeWarning: found in sys.modules ... prior to execution). `main` stays
available as a lazy attribute instead.
"""

import os
import subprocess
import sys

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


def _run(code):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [os.path.abspath(SRC)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env=env, timeout=120,
    )


def test_importing_package_does_not_load_main():
    r = _run("import cat_talker, sys; print('cat_talker.main' in sys.modules)")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "False", \
        "import cat_talker eagerly loaded cat_talker.main"


def test_main_still_importable_from_package():
    r = _run("from cat_talker import main; print(callable(main))")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "True", \
        "public API `from cat_talker import main` broke"
