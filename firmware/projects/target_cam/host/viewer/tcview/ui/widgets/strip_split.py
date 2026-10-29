"""An image over its ReadoutStrip (§4.4), as Image & calibration and Label
frames both lay them out: one vertical splitter whose lower part is the strip
in a scroll area, sized so the image keeps most of a small screen."""
from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QFrame, QScrollArea, QWidget

from .readout import ReadoutStrip
from .splitter import GripSplitter


class StripSplitter(GripSplitter):
    """The strip's height is what its cards need, but never more than
    `max_share` of the splitter, so the image keeps most of a small screen.
    Once the user drags the handle, their height is kept instead. Collapsed,
    the strip is its header alone and the image takes the rest."""

    def __init__(self, view: QWidget, strip: ReadoutStrip, max_share: float = 0.4, parent=None):
        super().__init__(Qt.Orientation.Vertical, parent)
        self.view, self.strip = view, strip
        self.max_share = max_share
        self._strip_h = 0                   # the height the user dragged the strip to, if any
        self.strip_scroll = QScrollArea()
        self.strip_scroll.setWidgetResizable(True)
        self.strip_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.strip_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.strip_scroll.setWidget(strip)
        self.strip_scroll.setMinimumHeight(strip.header.sizeHint().height() + 8)
        strip.header.toggled.connect(self._toggled)
        self.addWidget(view)
        self.addWidget(self.strip_scroll)
        self.setStretchFactor(0, 1)
        self.setStretchFactor(1, 0)
        self.setCollapsible(0, False)
        self.setCollapsible(1, False)
        self.splitterMoved.connect(self._dragged)

    @property
    def strip_height(self) -> int:
        """The height the user dragged the strip to, 0 if they never did."""
        return self._strip_h

    @strip_height.setter
    def strip_height(self, h: int):
        self._strip_h = max(0, int(h))
        self.size_strip()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        QTimer.singleShot(0, self.size_strip)       # once the sizes have settled

    def _dragged(self, *_):
        if self.strip.is_expanded():
            self._strip_h = self.sizes()[1]

    def size_strip(self):
        total = sum(self.sizes())
        if total <= 0:
            return
        lo = self.strip_scroll.minimumHeight()
        if not self.strip.is_expanded():
            h = lo
        elif self._strip_h:
            h = max(lo, min(self._strip_h, total - self.view.minimumHeight()))
        else:
            need = self.strip.heightForWidth(self.strip_scroll.viewport().width()) + 4
            h = max(lo, min(need, int(total * self.max_share)))
        if self.sizes()[1] != h:
            self.setSizes([total - h, h])

    def _toggled(self, on: bool):
        self.strip_scroll.setMaximumHeight(16777215 if on else self.strip_scroll.minimumHeight())
        self.size_strip()
