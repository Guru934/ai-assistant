"""Focused test: the shipped desktop entry must identify the app exactly as
main.py registers itself with Qt, so xdg-desktop-portal can resolve the
app ID (no Qt import needed - pure text parsing)."""

import configparser
import os
import re

REPO = os.path.join(os.path.dirname(__file__), "..")
DESKTOP_FILE = os.path.join(REPO, "assets", "cat-talker-overlay.desktop")
MAIN_PY = os.path.join(REPO, "src", "cat_talker", "main.py")


def _qt_names():
    src = open(MAIN_PY).read()
    app_match = re.search(r'setApplicationName\("([^"]+)"\)', src)
    desktop_match = re.search(r'setDesktopFileName\("([^"]+)"\)', src)
    assert app_match and desktop_match, "Qt app-ID registration not found in main.py"
    return app_match.group(1), desktop_match.group(1)


def _entry():
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(DESKTOP_FILE)
    return parser


def test_desktop_file_exists_and_parses():
    assert os.path.isfile(DESKTOP_FILE), "shipped desktop entry missing"
    entry = _entry()
    assert "Desktop Entry" in entry.sections()


def test_desktop_id_matches_qt_registration():
    """Desktop-file ID (basename) must equal setDesktopFileName, and
    StartupWMClass must equal setApplicationName (verified live via xprop:
    WM_CLASS class is 'cat-talker-overlay')."""
    app_name, desktop_name = _qt_names()
    assert os.path.basename(DESKTOP_FILE) == desktop_name + ".desktop"
    entry = _entry()["Desktop Entry"]
    assert entry["StartupWMClass"] == app_name
    for key in ("Type", "Name", "Exec"):
        assert entry.get(key), "missing required key %r" % key
    assert entry["Type"] == "Application"
    assert "cat_talker.main" in entry["Exec"], \
        "Exec must launch this application's main module"


def test_desktop_entry_installed_where_portal_looks():
    """The entry must exist under an XDG applications dir, otherwise the
    portal cannot resolve the app ID at runtime."""
    data_home = os.environ.get(
        "XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    data_dirs = [data_home] + os.environ.get(
        "XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":")
    found = any(
        os.path.isfile(os.path.join(d, "applications", "cat-talker-overlay.desktop"))
        for d in data_dirs
    )
    assert found, "cat-talker-overlay.desktop not installed in any XDG applications dir"
