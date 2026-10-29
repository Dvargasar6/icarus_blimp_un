"""Remembering the layout between runs (§9), in the viewer's QSettings.

Everything here is a preference, never data: a value that is missing, of
the wrong type or out of range is skipped, and the widget keeps the default
it was built with. The store is an .ini file a person can edit, and an old
viewer's keys, or a window saved on a bigger screen, must never stop the
viewer from opening."""
from __future__ import annotations

from typing import Callable, Optional

from PyQt6.QtCore import QByteArray, QSettings, Qt
from PyQt6.QtWidgets import QApplication, QSplitter, QWidget

from .widgets import ZOOMS, PixelImageView, SegmentedButtons, StripSplitter


def read(store: QSettings, key: str, typ, ok: Callable[[object], bool] = lambda _v: True):
    """The stored value as `typ`, or None if it is absent or unusable."""
    if not store.contains(key):
        return None
    try:
        v = store.value(key, type=typ)
    except (TypeError, ValueError):
        return None
    try:
        return v if ok(v) else None
    except (TypeError, ValueError):
        return None


def read_int(store: QSettings, key: str, lo: int, hi: int) -> Optional[int]:
    return read(store, key, int, lambda v: lo <= v <= hi)


def read_bool(store: QSettings, key: str) -> Optional[bool]:
    # QSettings turns any string into a bool; only what it wrote counts.
    raw = store.value(key) if store.contains(key) else None
    if isinstance(raw, bool):
        return raw
    return {"true": True, "false": False}.get(str(raw).lower()) if raw is not None else None


# ---- splitters -------------------------------------------------------------------

def save_splitter(store: QSettings, key: str, sp: QSplitter):
    if isinstance(sp, StripSplitter):
        # Its sizes are worked out again on every resize; what the user
        # chose is the strip's state and the height they dragged it to.
        store.setValue(key + "/strip_open", sp.strip.is_expanded())
        store.setValue(key + "/strip_h", sp.strip_height)
    else:
        store.setValue(key, sp.saveState())


def restore_splitter(store: QSettings, key: str, sp: QSplitter):
    if isinstance(sp, StripSplitter):
        on = read_bool(store, key + "/strip_open")
        if on is not None:
            sp.strip.set_expanded(on)
        h = read_int(store, key + "/strip_h", 0, 10000)
        if h is not None:
            sp.strip_height = h
        return
    st = read(store, key, QByteArray, lambda v: not v.isEmpty())
    if st is not None:
        before = sp.saveState()
        if not sp.restoreState(st) or sp.count() != len(sp.sizes()):
            sp.restoreState(before)


# ---- sections, zoom ---------------------------------------------------------------

def save_sections(store: QSettings, sections: dict):
    for name, s in sections.items():
        store.setValue("open/" + name, s.is_expanded())


def restore_sections(store: QSettings, sections: dict):
    for name, s in sections.items():
        on = read_bool(store, "open/" + name)
        if on is not None:
            s.set_expanded(on)


def save_zoom(store: QSettings, view: PixelImageView):
    store.setValue("zoom", view.zoom())


def restore_zoom(store: QSettings, seg: SegmentedButtons):
    """Through the zoom buttons, which set the view and stay in step with it."""
    z = read(store, "zoom", str, lambda v: v in ZOOMS)
    if z is not None:
        seg.set_value(z)


# ---- windows -----------------------------------------------------------------------

def save_geometry(store: QSettings, key: str, win: QWidget):
    store.setValue(key, win.saveGeometry())


def restore_geometry(store: QSettings, key: str, win: QWidget, w: int, h: int) -> bool:
    """The saved size and place if it fits the screen the window opens on,
    else w x h cut to that screen. True if the saved one was used."""
    screen = win.screen() or QApplication.primaryScreen()
    avail = screen.availableGeometry()
    g = read(store, key, QByteArray, lambda v: not v.isEmpty())
    if g is not None and win.restoreGeometry(g):
        r = win.geometry()
        if (r.width() <= avail.width() and r.height() <= avail.height()
                and avail.intersects(r)):
            return True
        win.setWindowState(Qt.WindowState.WindowNoState)
        win.move(avail.topLeft())
    win.resize(min(w, avail.width()), min(h, avail.height()))
    return False
