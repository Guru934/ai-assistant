"""Focused tests for the in-window speech bubble.

The bubble is painted inside the 320x320 window (never a clipped child
or a compositor-fought top-level): box geometry from _bubble_box(),
avatar shifted down only while visible, fade/show/hide preserved.
Qt offscreen throughout.
"""

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from PyQt6.QtWidgets import QApplication

_qt_app = QApplication.instance() or QApplication([])

from cat_talker.main import (
    BUBBLE_AVATAR_CY,
    BUBBLE_AVATAR_SCALE,
    BUBBLE_TEXT_MAX_H,
    BUBBLE_TEXT_MAX_W,
    BUBBLE_TOP,
    count_overlay_windows,
    get_overlay_window,
)


def _window():
    window = get_overlay_window()
    # Production invariant is 320x320; other suites may resize the
    # singleton, so normalize before geometry assertions.
    window.resize(320, 320)
    window.show()
    QApplication.processEvents()
    return window


def _drain():
    QApplication.processEvents()
    QApplication.processEvents()


SHORT = "Sure!"
MEDIUM = "I found the file and opened it for you. Let me know what you want to change."
LONG = ("Here is the full report of everything I did on your desktop "
        "just now, step by step. " * 12).strip()


def test_short_text_box_inside_window():
    window = _window()
    window._on_bubble(SHORT)
    assert window.bubble_text == SHORT
    x, y, w, h = window._bubble_box()
    # Centered full-width box by design: assert the centering contract,
    # not a magic constant. The overlay is a process-global singleton
    # whose width other suites may leave non-320 (resize-then-read
    # races), so (12, 8) only holds at exactly 320px wide.
    assert w == BUBBLE_TEXT_MAX_W + 28
    assert (x, y) == ((window.width() - w) // 2, BUBBLE_TOP)
    assert 0 < w <= BUBBLE_TEXT_MAX_W + 28
    assert 0 < h <= 140
    # Fully inside the renderable window: no negative/clipped coords.
    assert x >= 0 and y >= 0
    assert x + w <= window.width()
    assert y + h <= window.height()


def test_multiline_text_wraps_in_box():
    window = _window()
    window._on_bubble(MEDIUM)
    x, y, w, h = window._bubble_box()
    assert w <= BUBBLE_TEXT_MAX_W + 28
    assert h > 60, "medium text must wrap to several lines"
    assert y + h <= window.height()
    # Narrower than the cap is impossible: full-width box by design.
    assert w == BUBBLE_TEXT_MAX_W + 28


def test_long_text_bounded_above_avatar():
    window = _window()
    window._on_bubble(LONG)
    assert window.bubble_text == LONG  # text itself never altered
    x, y, w, h = window._bubble_box()
    assert h <= BUBBLE_TEXT_MAX_H + 20 + 4
    assert y + h <= window.height()
    # Avatar zone starts below the box: no overlap by construction.
    avatar_top = BUBBLE_AVATAR_CY - 90 * BUBBLE_AVATAR_SCALE
    assert y + h <= avatar_top, ((x, y, w, h), avatar_top)


def test_empty_text_gives_zero_box():
    window = _window()
    window._on_bubble("")
    assert window._bubble_box() == (0, 0, 0, 0)


def test_fade_flag_lifecycle():
    window = _window()
    window._on_bubble(SHORT)
    window._set_bubble_opacity(1.0)
    assert window._bubble_visible is True
    window._set_bubble_opacity(0.0)
    assert window._bubble_visible is False
    assert window.bubble_text == SHORT  # fade keeps the text


def test_stale_fade_timer_ignores_newer_bubble():
    window = _window()
    window._on_bubble("first")
    window._set_bubble_opacity(1.0)
    window._on_bubble("second")
    window._set_bubble_opacity(1.0)
    old_gen = window._bubble_gen - 1
    window._fade_bubble(old_gen)  # stale timer must not fade "second"
    assert window._bubble_visible is True
    assert window.bubble_text == "second"


def test_hide_show_keeps_bubble_state():
    window = _window()
    window._on_bubble(SHORT)
    window._set_bubble_opacity(1.0)
    window.hide()
    _drain()
    assert window.bubble_text == SHORT
    window.show()
    _drain()
    assert window._bubble_visible is True
    window.position_bottom_center()


def test_paint_runs_with_and_without_bubble():
    """paintEvent must not raise in either layout mode (avatar full-size
    or shifted); visual diff is verified by screenshot, not pixels."""
    window = _window()
    window._set_bubble_opacity(0.0)
    window.repaint()
    _drain()
    window._on_bubble(MEDIUM)
    window._set_bubble_opacity(1.0)
    window.repaint()
    _drain()
    window._set_bubble_opacity(0.0)
    window.repaint()
    _drain()


def test_styling_contrast_constants():
    from cat_talker.main import COLORS
    assert COLORS["bubble_text"].name() == "#f0f0f5"
    bg = COLORS["bubble_bg"]
    assert bg.alpha() >= 200  # semi-opaque over bright wallpapers
    assert (bg.red(), bg.green(), bg.blue()) == (30, 30, 40)


def test_single_window_invariant_kept():
    _window()
    assert count_overlay_windows() == 1
    from PyQt6.QtWidgets import QLabel
    strays = [w for w in QApplication.topLevelWidgets()
              if isinstance(w, QLabel)]
    assert strays == [], "no top-level label windows may exist"
