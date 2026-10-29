"""PixelImageView (docs/GUI_REDESIGN.md §4.6): the pixel under the cursor and
the region box must stay right at every zoom, scrolled or not, since the
user reads colours and calibrates through them."""
import numpy as np
import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QMouseEvent, QWheelEvent
from PyQt6.QtWidgets import QApplication

from tcview.ui import theme
from tcview.ui.widgets import ZOOMS, PixelImageView
from tcview.ui.widgets.pixel_view import fit_scale


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication([])
    theme.apply(a)
    return a


@pytest.fixture
def view(app):
    v = PixelImageView(160, 120)
    v.resize(700, 500)
    v.set_image(np.zeros((120, 160, 3), dtype=np.uint8))
    v.show()
    app.processEvents()
    yield v
    v.close()


def mouse(view, kind, img_xy, button=Qt.MouseButton.NoButton):
    """A mouse event at the centre of image pixel img_xy (or at an exact image
    point for a float pair), delivered to the canvas as Qt would."""
    x, y = img_xy
    if isinstance(x, int):
        x, y = x + 0.5, y + 0.5
    pos = view.to_widget(QPointF(x, y))
    buttons = button if kind != QEvent.Type.MouseButtonRelease else Qt.MouseButton.NoButton
    if kind == QEvent.Type.MouseMove and button != Qt.MouseButton.NoButton:
        buttons, button = button, Qt.MouseButton.NoButton
    e = QMouseEvent(kind, pos, view.canvas.mapToGlobal(pos), button, buttons,
                    Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(view.canvas, e)


def test_fit_prefers_whole_pixels():
    from PyQt6.QtCore import QSize
    assert fit_scale(QSize(640, 480), 160, 120) == 4
    assert fit_scale(QSize(670, 500), 160, 120) == 4      # 4x fills 95 %: integer
    assert 2 < fit_scale(QSize(560, 420), 160, 120) < 4   # 3x fills only 86 %: fractional


@pytest.mark.parametrize("zoom", ZOOMS)
def test_hover_reports_the_pixel_under_the_cursor(app, view, zoom):
    view.set_zoom(zoom)
    app.processEvents()
    if zoom != "fit":
        # Scrolled to the far corner, the mapping must still hold.
        view.horizontalScrollBar().setValue(view.horizontalScrollBar().maximum())
        view.verticalScrollBar().setValue(view.verticalScrollBar().maximum())
        app.processEvents()
        assert view.horizontalScrollBar().maximum() > 0
    seen = []
    view.hovered.connect(lambda x, y: seen.append((x, y)))
    for px in ((0, 0), (159, 119), (100, 70), (157, 94)):
        mouse(view, QEvent.Type.MouseMove, px)
        assert seen[-1] == px
    n = len(seen)
    mouse(view, QEvent.Type.MouseMove, (157.9, 94.9))      # same pixel: no repeat
    assert len(seen) == n
    left = []
    view.left.connect(lambda: left.append(1))
    QApplication.sendEvent(view.canvas, QEvent(QEvent.Type.Leave))
    assert left == [1]


@pytest.mark.parametrize("zoom", ZOOMS)
def test_region_moves_and_resizes_in_whole_pixels(app, view, zoom):
    view.set_zoom(zoom)
    view.set_region(72, 52, 16, 16, emit=False)
    app.processEvents()
    got = []
    view.regionChanged.connect(got.append)
    # Make sure the box is on screen when zoomed in.
    view.ensureVisible(int(view.to_widget(QPointF(80, 60)).x()),
                       int(view.to_widget(QPointF(80, 60)).y()), 200, 200)
    app.processEvents()

    L = Qt.MouseButton.LeftButton
    mouse(view, QEvent.Type.MouseButtonPress, (80, 60), L)
    mouse(view, QEvent.Type.MouseMove, (83, 58), L)
    mouse(view, QEvent.Type.MouseButtonRelease, (83, 58), L)
    assert view.region() == (75, 50, 16, 16) and got[-1] == (75, 50, 16, 16)

    # Bottom-right handle: the top-left corner stays.
    mouse(view, QEvent.Type.MouseButtonPress, (91.0, 66.0), L)
    mouse(view, QEvent.Type.MouseMove, (95.2, 70.4), L)
    mouse(view, QEvent.Type.MouseButtonRelease, (95.2, 70.4), L)
    assert view.region() == (75, 50, 20, 20)

    # Top-left handle: the bottom-right corner stays, and the box keeps >= 1 px.
    mouse(view, QEvent.Type.MouseButtonPress, (75.0, 50.0), L)
    mouse(view, QEvent.Type.MouseMove, (120.0, 90.0), L)
    mouse(view, QEvent.Type.MouseButtonRelease, (120.0, 90.0), L)
    assert view.region() == (94, 69, 1, 1)


def test_region_is_clamped_to_the_image(view):
    view.set_region(150, 115, 16, 16)
    assert view.region() == (144, 104, 16, 16)
    view.set_region(-5, -5, 500, 500)
    assert view.region() == (0, 0, 160, 120)


def test_ctrl_wheel_zooms_about_the_cursor(app, view):
    zs = []
    view.zoomChanged.connect(zs.append)
    a = QPoint(200, 150)
    before = view.to_image(QPointF(a))
    e = QWheelEvent(QPointF(a), QPointF(view.viewport().mapToGlobal(a)), QPoint(), QPoint(0, 120),
                    Qt.MouseButton.NoButton, Qt.KeyboardModifier.ControlModifier,
                    Qt.ScrollPhase.NoScrollPhase, False)
    # Sent where Qt sends it: to the canvas, which passes it up to the viewport
    # (at fit the canvas fills the viewport, so the coordinates are the same).
    QApplication.sendEvent(view.canvas, e)
    app.processEvents()
    assert view.zoom() == "2" and zs == ["2"]
    sx, sy = view.horizontalScrollBar().value(), view.verticalScrollBar().value()
    after = view.to_image(QPointF(a) + QPointF(sx, sy))
    # Within a screen pixel of where it was.
    assert abs(after.x() - before.x()) * view.scale <= 1.5
    assert abs(after.y() - before.y()) * view.scale <= 1.5


def test_fit_has_no_scroll_bars_and_zoom_does(app, view):
    assert view.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    view.set_zoom("4")
    app.processEvents()
    assert view.horizontalScrollBar().maximum() > 0 and view.verticalScrollBar().maximum() > 0
    assert view.scale == 4 * fit_scale(view.maximumViewportSize(), 160, 120)
