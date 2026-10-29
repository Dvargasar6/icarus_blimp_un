"""
The viewer's look, in one place: colour tokens, the dark palette, the
application stylesheet and the two fonts (docs/GUI_REDESIGN.md §3).

Everything visual reads its colours from TOKENS, so a colour changes here and
nowhere else. Variants are dynamic properties, not per-widget stylesheets:

    role = "primary" | "danger"         on QPushButton
    tone = "ok" | "warn" | "err" | "info" | "neutral"
    mono = true                         data font (numbers, readouts, logs)
    small = true                        26 px buttons ("size" is taken: it is
                                        QWidget's own geometry property)
    caption = true                      12 px semibold UPPERCASE, muted

After changing one at run time call repolish(widget): Qt only re-reads a
stylesheet selector when told to.

Fonts are never set in the stylesheet's QWidget rule. A stylesheet font
overrides setFont() on every widget it matches, and the old UI sets a 26 pt
font on the Flight verdict; the base font goes in through app.setFont()
instead, and the stylesheet only adds the mono and caption variants.
"""
from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QPainterPath, QPalette, QPen
from PyQt6.QtWidgets import QApplication, QProxyStyle, QStyle, QWidget

TOKENS = {
    # surfaces
    "bg": "#16181c",
    "panel": "#1e2126",
    "raised": "#272b31",
    "hover": "#2f343b",
    "pressed": "#33383f",
    "input": "#16181c",
    "console_bg": "#121417",
    "plot_bg": "#0f1114",
    "splitter": "#1a1c20",
    "selection": "#1f5a66",
    # lines
    "border": "#2e3238",
    "border_strong": "#3a4048",
    "divider": "#2a2e34",
    # text
    "text": "#e7e9ec",
    "text_muted": "#a3aab4",
    "text_faint": "#8b929c",
    # accent
    "accent": "#4fc3d6",
    "on_accent": "#0b1a1e",
    # tones
    "ok_text": "#9fe0bd", "ok_bg": "#1b3027", "ok_border": "#2f5a45", "ok_dot": "#4fd18b",
    "warn_text": "#f0c784", "warn_bg": "#33291a", "warn_border": "#6a5226", "warn_dot": "#f0b44c",
    "err_text": "#f4b3a8", "err_bg": "#3a2220", "err_border": "#6b3a33", "err_dot": "#f07a6a",
    "info_text": "#bfe9f0", "info_bg": "#17303a", "info_border": "#285866", "info_dot": "#4fc3d6",
    # data
    "label_balloon": "#4fc3d6",
    # The old tab's orange (255, 160, 0): opposite the balloon cyan, so
    # background paint stands out from balloon paint at a glance.
    "label_not": "#ffa000",
    "series_az": "#f2c14e",
    "series_el": "#4fc3d6",
    "series_range": "#7ad67a",
    "grid_minor": "#1d2126",
    "grid_major": "#2a2f36",
    "axis_zero": "#454c55",
    "verdict_target": "#2f7a45",
}

# The spec gives warn and info no dot colour; warn_dot is warn_text's hue made
# stronger, info_dot is the accent.
TONES = ("ok", "warn", "err", "info", "neutral")


def tone_colours(tone: str) -> dict:
    """text / bg / border / dot for a tone; "neutral" is a plain raised chip."""
    if tone not in TONES or tone == "neutral":
        return {"text": TOKENS["text_muted"], "bg": TOKENS["raised"],
                "border": TOKENS["border_strong"], "dot": TOKENS["text_faint"]}
    return {k: TOKENS["%s_%s" % (tone, k)] for k in ("text", "bg", "border", "dot")}


def qcolor(name: str, alpha: int = 255) -> QColor:
    c = QColor(TOKENS.get(name, name))
    c.setAlpha(alpha)
    return c


# ---- fonts -------------------------------------------------------------------

UI_FAMILIES = ("IBM Plex Sans", "Inter", "Noto Sans", "DejaVu Sans")
MONO_FAMILIES = ("IBM Plex Mono", "DejaVu Sans Mono", "Noto Sans Mono", "Liberation Mono")
UI_PX = 13
MONO_PX = 12


def pick_family(candidates, fallback: QFontDatabase.SystemFont) -> str:
    have = set(QFontDatabase.families())
    for fam in candidates:
        if fam in have:
            return fam
    return QFontDatabase.systemFont(fallback).family()


_families: dict = {}


def ui_family() -> str:
    if "ui" not in _families:
        _families["ui"] = pick_family(UI_FAMILIES, QFontDatabase.SystemFont.GeneralFont)
    return _families["ui"]


def mono_family() -> str:
    if "mono" not in _families:
        _families["mono"] = pick_family(MONO_FAMILIES, QFontDatabase.SystemFont.FixedFont)
    return _families["mono"]


def ui_font(px: int = UI_PX, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    f = QFont(ui_family())
    f.setPixelSize(px)
    f.setWeight(weight)
    return f


def mono_font(px: int = MONO_PX) -> QFont:
    f = QFont(mono_family())
    f.setPixelSize(px)
    f.setStyleHint(QFont.StyleHint.Monospace)
    return f


def caption_font() -> QFont:
    """Group captions: 12 px semibold UPPERCASE with 0.05em tracking. The
    tracking cannot be said in QSS, so captions take this font directly."""
    f = ui_font(12, QFont.Weight.DemiBold)
    f.setCapitalization(QFont.Capitalization.AllUppercase)
    f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 105)
    return f


# ---- palette and stylesheet --------------------------------------------------

def palette() -> QPalette:
    t = TOKENS
    p = QPalette()
    R = QPalette.ColorRole
    for role, tok in ((R.Window, "bg"), (R.WindowText, "text"), (R.Base, "input"),
                      (R.AlternateBase, "panel"), (R.ToolTipBase, "raised"),
                      (R.ToolTipText, "text"), (R.PlaceholderText, "text_faint"),
                      (R.Text, "text"), (R.Button, "raised"), (R.ButtonText, "text"),
                      (R.BrightText, "err_text"), (R.Highlight, "selection"),
                      (R.HighlightedText, "text"), (R.Link, "accent"),
                      (R.LinkVisited, "accent"), (R.Light, "border_strong"),
                      (R.Midlight, "border"), (R.Mid, "border"), (R.Dark, "bg"),
                      (R.Shadow, "console_bg")):
        p.setColor(role, QColor(t[tok]))
    D = QPalette.ColorGroup.Disabled
    for role in (R.WindowText, R.Text, R.ButtonText):
        p.setColor(D, role, QColor(t["text_faint"]))
    p.setColor(D, R.Button, QColor(t["panel"]))
    p.setColor(D, R.Base, QColor(t["panel"]))
    p.setColor(QPalette.ColorGroup.Inactive, R.Highlight, QColor(t["selection"]))
    return p


QSS = """
QLabel, QCheckBox, QRadioButton {{ background: transparent; }}
QToolTip {{ background: {raised}; color: {text}; border: 1px solid {border_strong}; padding: 4px 6px; }}

QFrame#Panel, QWidget#Panel {{ background: {panel}; }}
QFrame#Card {{ background: {panel}; border: 1px solid {border}; border-radius: 8px; }}
QFrame#Pane {{ background: transparent; border: 0; }}
QFrame[divider="true"] {{ background: {divider}; border: 0; }}

*[mono="true"] {{ font-family: "{mono}"; font-size: {mono_px}px; }}
QLabel[muted="true"] {{ color: {text_muted}; }}
QLabel[faint="true"] {{ color: {text_faint}; }}

QGroupBox {{ background: {panel}; border: 1px solid {border}; border-radius: 8px;
            margin-top: 22px; padding: 10px 8px 8px 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left;
                   left: 8px; top: 2px; padding: 0 4px; color: {text_muted}; font-weight: 600; }}

QPushButton, QToolButton {{ background: {raised}; color: {text}; border: 1px solid {border_strong};
                           border-radius: 6px; padding: 0 12px; min-height: 28px; }}
QPushButton:hover, QToolButton:hover {{ background: {hover}; }}
QPushButton:pressed, QToolButton:pressed {{ background: {pressed}; }}
QPushButton:checked, QToolButton:checked {{ background: {selection}; border-color: {accent}; }}
QPushButton:focus, QToolButton:focus {{ border-color: {accent}; }}
QPushButton:disabled, QToolButton:disabled {{ background: {panel}; color: {text_faint}; border-color: {border}; }}
QPushButton[role="primary"] {{ background: {accent}; border-color: {accent}; color: {on_accent}; font-weight: 600; }}
QPushButton[role="primary"]:hover {{ background: #6fd0e0; }}
QPushButton[role="primary"]:pressed {{ background: #2f8fa0; border-color: #2f8fa0; }}
QPushButton[role="primary"]:disabled {{ background: {selection}; border-color: {selection}; color: {text_faint}; }}
QPushButton[role="danger"] {{ background: transparent; border-color: {err_border}; color: {err_text}; }}
QPushButton[role="danger"]:hover {{ background: {err_bg}; }}
QPushButton[role="danger"]:pressed {{ background: {err_border}; }}
/* min/max-height size the content rect: 24 + the 1 px border = 26 px */
QPushButton[small="true"], QToolButton[small="true"] {{ min-height: 24px; max-height: 24px; padding: 0 8px; }}
QToolButton[iconOnly="true"] {{ min-height: 24px; max-height: 24px; min-width: 24px; max-width: 24px; padding: 0; }}
QToolButton[flat="true"] {{ background: transparent; border-color: transparent; }}
QToolButton[flat="true"]:hover {{ background: {hover}; }}

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit {{
    background: {input}; color: {text}; border: 1px solid {border_strong}; border-radius: 6px;
    padding: 2px 8px; selection-background-color: {selection}; selection-color: {text}; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ min-height: 24px; }}
QPlainTextEdit, QTextEdit {{ padding: 4px; }}
QPlainTextEdit#Console {{ background: {console_bg}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QPlainTextEdit:focus, QTextEdit:focus {{ border-color: {accent}; }}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{
    color: {text_faint}; border-color: {border}; }}
QComboBox::drop-down {{ border: 0; width: 20px; }}
QComboBox QAbstractItemView {{ background: {raised}; border: 1px solid {border_strong};
                              selection-background-color: {selection}; outline: 0; }}
QSpinBox, QDoubleSpinBox {{ padding-right: 18px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::down-button {{
    background: transparent; border: 0; width: 16px; }}

QListView, QListWidget, QTreeView, QTableView, QTableWidget {{
    background: {input}; border: 1px solid {border}; border-radius: 6px;
    selection-background-color: {selection}; selection-color: {text}; gridline-color: {divider};
    outline: 0; }}
QListView::item:hover, QListWidget::item:hover {{ background: {raised}; }}
QListView::item:selected, QListWidget::item:selected {{ background: {selection}; }}
QHeaderView::section {{ background: {panel}; color: {text_muted}; border: 0;
                       border-bottom: 1px solid {divider}; padding: 4px 6px; font-weight: 600; }}
QTableCornerButton::section {{ background: {panel}; border: 0; }}

QTabWidget::pane {{ border: 0; border-top: 1px solid {border}; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{ background: transparent; color: {text_muted}; padding: 10px 14px; border: 0;
               border-bottom: 2px solid transparent; }}
QTabBar::tab:hover {{ color: {text}; }}
QTabBar::tab:selected {{ color: {text}; font-weight: 600; border-bottom: 2px solid {accent}; }}

QSplitter::handle {{ background: {splitter}; border: 1px solid {border}; }}

QSlider {{ min-height: 18px; }}
QSlider::groove:horizontal {{ height: 4px; background: {border_strong}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {accent}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {text}; width: 12px; margin: -5px 0; border-radius: 6px; }}
QSlider::handle:horizontal:hover {{ background: #ffffff; }}
QSlider::groove:horizontal:disabled {{ background: {border}; }}
QSlider::sub-page:horizontal:disabled {{ background: {border_strong}; }}

QScrollArea {{ background: transparent; border: 0; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {border_strong}; border-radius: 4px; min-height: 24px; margin: 1px; }}
QScrollBar::handle:horizontal {{ background: {border_strong}; border-radius: 4px; min-width: 24px; margin: 1px; }}
QScrollBar::handle:hover {{ background: #4a515b; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: 0; background: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

QStatusBar {{ background: {panel}; border-top: 1px solid {border}; color: {text_muted}; }}
QWidget#ConnBar {{ background: {panel}; border-bottom: 1px solid {border}; }}
QWidget#ConsoleHeader, QWidget#ConsoleBody {{ background: {panel}; }}
QLineEdit#LastLine {{ background: transparent; border: 0; padding: 0 2px; color: {text_muted}; }}
QStatusBar::item {{ border: 0; }}
QMenu {{ background: {raised}; border: 1px solid {border_strong}; padding: 4px; }}
QMenu::item {{ padding: 4px 16px; border-radius: 4px; }}
QMenu::item:selected {{ background: {selection}; }}
QDialog, QMessageBox {{ background: {panel}; }}
"""


class ThemeStyle(QProxyStyle):
    """Fusion, with check box and radio indicators drawn from the tokens.
    Fusion draws them in the palette's Base colour, which is the window
    colour here, so an unchecked box vanished into the panel. QSS could
    restyle them, but a QSS check mark must be an image file; painting
    needs no files."""

    def __init__(self):
        super().__init__("Fusion")

    def pixelMetric(self, m, opt=None, w=None):
        if m == QStyle.PixelMetric.PM_DockWidgetSeparatorExtent:
            return 7                    # the same grab width as GripSplitter (§2)
        return super().pixelMetric(m, opt, w)

    def drawPrimitive(self, el, opt, p, w=None):
        PE = QStyle.PrimitiveElement
        if el == PE.PE_IndicatorDockWidgetResizeHandle:
            return self._grip(opt, p)
        if el not in (PE.PE_IndicatorCheckBox, PE.PE_IndicatorRadioButton):
            return super().drawPrimitive(el, opt, p, w)
        S = QStyle.StateFlag
        st = opt.state
        on = bool(st & S.State_On)
        partial = bool(st & S.State_NoChange)
        enabled = bool(st & S.State_Enabled)
        hot = bool(st & (S.State_MouseOver | S.State_HasFocus))
        side = min(opt.rect.width(), opt.rect.height()) - 1
        r = QRectF(opt.rect.x() + 0.5, opt.rect.y() + 0.5, side, side)
        p.save()
        p.setRenderHint(p.RenderHint.Antialiasing)
        edge = "accent" if (on or partial or hot) and enabled else "text_faint"
        if not enabled:
            edge = "border_strong"
        p.setPen(QPen(qcolor(edge), 1))
        if el == PE.PE_IndicatorRadioButton:
            p.setBrush(qcolor("input"))
            p.drawEllipse(r)
            if on:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(qcolor("accent" if enabled else "text_faint"))
                p.drawEllipse(r.center(), side * 0.26, side * 0.26)
        else:
            p.setBrush(qcolor(("accent" if enabled else "border_strong") if on or partial
                              else "input"))
            p.drawRoundedRect(r, 3, 3)
            mark = QPen(qcolor("on_accent"), 1.8, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            p.setPen(mark)
            if on:
                path = QPainterPath(QPointF(r.left() + side * 0.24, r.top() + side * 0.52))
                path.lineTo(r.left() + side * 0.43, r.top() + side * 0.70)
                path.lineTo(r.left() + side * 0.77, r.top() + side * 0.31)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(path)
            elif partial:
                y = r.center().y()
                p.drawLine(QPointF(r.left() + side * 0.27, y), QPointF(r.right() - side * 0.27, y))
        p.restore()


    @staticmethod
    def _grip(opt, p):
        """The dock separator (the console's top edge), drawn like
        GripSplitter's handle: a dark bar with five dots."""
        r = opt.rect
        p.save()
        p.fillRect(r, qcolor("splitter"))
        p.setRenderHint(p.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        hot = bool(opt.state & QStyle.StateFlag.State_MouseOver)
        p.setBrush(qcolor("text_faint" if hot else "border_strong"))
        c = QPointF(r.center())
        across = r.width() > r.height()         # a horizontal bar: dots side by side
        for k in (-8, -4, 0, 4, 8):
            p.drawEllipse(QPointF(c.x() + k, c.y()) if across else QPointF(c.x(), c.y() + k),
                          1.1, 1.1)
        p.restore()


def stylesheet() -> str:
    return QSS.format(mono=mono_family(), mono_px=MONO_PX, **TOKENS)


def apply(app: QApplication):
    """Fusion (ThemeStyle) + palette + base font + stylesheet, and pyqtgraph's defaults.
    Call before any window is built: pyqtgraph reads its background and
    foreground when each plot is created, not later."""
    app.setStyle(ThemeStyle())
    app.setPalette(palette())
    app.setFont(ui_font())
    app.setStyleSheet(stylesheet())
    try:
        import pyqtgraph as pg
        pg.setConfigOptions(background=TOKENS["plot_bg"], foreground=TOKENS["text_faint"])
    except ImportError:
        pass


# ---- per-widget variants -----------------------------------------------------

def repolish(w: QWidget):
    st = w.style()
    st.unpolish(w)
    st.polish(w)
    w.update()


def set_prop(w: QWidget, name: str, value) -> QWidget:
    if w.property(name) != value:
        w.setProperty(name, value)
        repolish(w)
    return w


def mono(w: QWidget) -> QWidget:
    """Data font. Works with or without the stylesheet: the font is set too."""
    w.setFont(mono_font())
    return set_prop(w, "mono", True)


def set_role(w: QWidget, role: str) -> QWidget:
    return set_prop(w, "role", role)


def set_tone(w: QWidget, tone: str) -> QWidget:
    return set_prop(w, "tone", tone)


def chip_css(tone: str) -> str:
    c = tone_colours(tone)
    return ("QLabel {{ color: {text}; background: {bg}; border: 1px solid {border};"
            " border-radius: 6px; padding: 2px 8px; }}").format(**c)
