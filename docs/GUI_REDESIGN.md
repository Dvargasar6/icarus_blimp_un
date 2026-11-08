# target_cam viewer — GUI redesign spec

This document describes a **GUI-only refactor** of the `target_cam` viewer (the Python/Qt UART terminal for the ESP32-CAM). It is written for Claude Code. Read all of it before changing anything, then follow the phased plan in §10.

The visual reference is a set of mockups made in Claude (one desktop board at 1440×900 and one small-screen board at 1280×720 per tab). This file is self-contained; the ASCII layouts below are the source of truth for structure, and §3 gives the exact colours and sizes.

---

## 0. Working agreement (read first)

- **Work in small, verified increments.** Do one phase from §10 at a time. At the end of each phase: run the app, take screenshots (see §11), summarise what changed, and **stop and wait for the user to confirm** before starting the next phase.
- Never send the user long multi-step command blocks; at most 3 commands at a time.
- Keep the app runnable after every phase. Commit each phase separately with a clear message.
- **Functionality must not change.** Same serial protocol, same camera commands, same CSV rows, same files written to `target_cam/data/`, same dataset format, same detector/classifier maths. Only layout, styling, widget choice and copy-ability change. If a change below would alter behaviour, ask first.
- Do not add new third-party dependencies. Use the Qt binding and plotting library the project already uses (detect them in Phase 0 — likely PyQt5/PyQt6/PySide6, and pyqtgraph for the plots).

---

## 1. Problems this refactor fixes

1. **Fixed layout.** Panels have fixed sizes; on smaller screens the window is cut off (e.g. on the Flight data tab the command box and status bar are not visible).
2. **Text cannot be copied.** The information printed under the image (pixel value, frame info, detection summary, link stats, link health, label stats, blob records, features) is in plain labels the user cannot select or copy.
3. **Panel toggle buttons** (Region / Colour / Blobs / Sensor / Flash on the right edge of tab 1) don't solve the size problem.
4. The **command line and console** live inside the tab area and get pushed off-screen.
5. Status and error messages are truncated in the status bar.

---

## 2. Global layout rules

- **Never** use `setFixedSize`, `setFixedWidth` or `setFixedHeight` on panels, and no large `setMinimumSize`/`setMinimumHeight` on plots or image views (≤ 160 px). Use layouts with stretch factors, `QSplitter`, and `QScrollArea`.
- The window must be fully usable at **1280×720** (and still usable at 1024×600): every control reachable, the command line and status bar always visible, on every tab.
- Side panels (inspectors) are `QScrollArea`s with `widgetResizable=True`, so on small screens they scroll instead of being clipped.
- Every splitter has a visible 7 px handle with a dotted grip and remembers its sizes (§9).
- Every block of information the user may want to paste somewhere is **selectable** (`Qt.TextSelectableByMouse`) **and** has a **copy button** (§4.2). Each group also has a "Copy all".

---

## 3. Design tokens

Put these in one module, `tcview/ui/theme.py` (§13), and build the application stylesheet (QSS) from them. Use the Fusion style as the base (`app.setStyle("Fusion")`) plus a dark `QPalette` built from the same tokens.

### 3.1 Colours

| Token | Hex | Use |
|---|---|---|
| `bg` | `#16181c` | window background, image/plot surroundings |
| `panel` | `#1e2126` | bars, inspectors, cards |
| `raised` | `#272b31` | buttons |
| `input` | `#16181c` | line edits, combos, spin boxes |
| `console_bg` | `#121417` | console log |
| `plot_bg` | `#0f1114` | plot backgrounds |
| `border` | `#2e3238` | panel borders |
| `border_strong` | `#3a4048` | buttons, inputs |
| `divider` | `#2a2e34` | separators inside panels |
| `text` | `#e7e9ec` | primary text |
| `text_muted` | `#a3aab4` | labels, secondary text |
| `text_faint` | `#8b929c` | hints, axis labels, units |
| `accent` | `#4fc3d6` | primary buttons, focus, selection, active tab underline, sliders |
| `on_accent` | `#0b1a1e` | text on accent buttons |
| `ok_text` / `ok_bg` / `ok_border` / `ok_dot` | `#9fe0bd` / `#1b3027` / `#2f5a45` / `#4fd18b` | connected, saved, applied, flight-identical |
| `warn_text` / `warn_bg` / `warn_border` | `#f0c784` / `#33291a` / `#6a5226` | non-zero junk/retries/timeouts, not flight-identical, unsaved |
| `err_text` / `err_bg` / `err_border` / `err_dot` | `#f4b3a8` / `#3a2220` / `#6b3a33` / `#f07a6a` | port not found, errors, destructive buttons |
| `info_text` / `info_bg` / `info_border` | `#bfe9f0` / `#17303a` / `#285866` | info banners (e.g. "stage 1 decides the pixels") |
| `label_balloon` | `#4fc3d6` | balloon class (label paint, blob badges, bars) |
| `label_not` | `#ffa000` | background / not-a-balloon class (the old tab's orange, kept for contrast with the balloon cyan) |
| `series_az` / `series_el` / `series_range` | `#f2c14e` / `#4fc3d6` / `#7ad67a` | Flight data time series |
| `grid_minor` / `grid_major` / `axis_zero` | `#1d2126` / `#2a2f36` / `#454c55` | plot grids |
| `verdict_target` | `#2f7a45` | TARGET tile background (white text) |

Rule: colours that must be told apart (balloon vs not) also differ in lightness, never only in hue.

### 3.2 Typography

- UI font: **IBM Plex Sans** if installed, otherwise the system sans. Base size **13 px** (≈ 10 pt). Section titles 13 px semibold; card titles 12 px semibold; group captions 12 px semibold UPPERCASE with 0.05em letter-spacing in `text_muted`.
- Data font (all numbers, readouts, console, file names, commands): **IBM Plex Mono** if installed, otherwise `DejaVu Sans Mono` / `monospace`, 12–12.5 px.
- Do not bundle or download fonts; just use `QFontDatabase` to pick the first available family.

### 3.3 Sizes

- Spacing: 4 / 6 / 8 / 12 / 16 px. Panel padding 12–16 px. Card padding 8–12 px.
- Corner radius: buttons/inputs 6 px, cards 8 px, chips/pills fully rounded.
- Buttons 30–32 px tall; small buttons 26 px; icon buttons 26–28 px square.
- Default inspector width 400 px (min 320). Flight readouts column 440 px (min 340).

### 3.4 Starter QSS (extend as needed)

```css
QWidget { background: #16181c; color: #e7e9ec; font-size: 13px; }
QFrame#Panel, QWidget#Panel { background: #1e2126; }
QFrame#Card { background: #1e2126; border: 1px solid #2e3238; border-radius: 8px; }
QPushButton, QToolButton { background: #272b31; border: 1px solid #3a4048; border-radius: 6px; padding: 0 12px; min-height: 30px; }
QPushButton:hover, QToolButton:hover { background: #2f343b; }
QPushButton:pressed, QToolButton:checked { background: #33383f; }
QPushButton[role="primary"] { background: #4fc3d6; border-color: #4fc3d6; color: #0b1a1e; font-weight: 600; }
QPushButton[role="danger"] { background: transparent; border-color: #6b3a33; color: #f4b3a8; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit { background: #16181c; border: 1px solid #3a4048; border-radius: 6px; padding: 2px 8px; selection-background-color: #1f5a66; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border-color: #4fc3d6; }
QTabBar::tab { background: transparent; color: #a3aab4; padding: 10px 14px; border: 0; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: #e7e9ec; font-weight: 600; border-bottom: 2px solid #4fc3d6; }
QSplitter::handle { background: #1a1c20; border: 1px solid #2e3238; }
QSlider::groove:horizontal { height: 4px; background: #3a4048; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #4fc3d6; border-radius: 2px; }
QSlider::handle:horizontal { background: #e7e9ec; width: 12px; margin: -5px 0; border-radius: 6px; }
QScrollBar:vertical { background: transparent; width: 10px; }
QScrollBar::handle:vertical { background: #3a4048; border-radius: 4px; min-height: 24px; }
QToolTip { background: #272b31; color: #e7e9ec; border: 1px solid #3a4048; }
```

Use dynamic properties (`setProperty("role", "primary")`, `"tone": "ok" | "warn" | "err" | "info"`) for variants, and re-polish after changing them.

---

## 4. Reusable widgets (build these first, in `tcview/ui/widgets/`)

### 4.1 `CollapsibleSection(title, summary="")`
- Header: full-width flat `QToolButton` with a chevron (▸ rotates to ▾ when open), bold title on the left and a **summary** label on the right (muted, mono, elided). The summary shows the current values so a collapsed section still tells you its state (e.g. `red · H 0° ±12° · S≥110 · V≥50`).
- Body: any widget/layout, hidden when collapsed.
- API: `set_summary(text)`, `set_expanded(bool)`, signal `toggled(bool)`.
- An inspector header has **Expand all / Collapse all** buttons.

### 4.2 `CopyButton(get_text: Callable[[], str], label=None)`
- Icon-only (copy icon) or labelled ("Copy", "Copy all"). On click: `QApplication.clipboard().setText(get_text())`, then show a check icon / "Copied" for 1.2 s (`QTimer.singleShot`).
- Always takes a callable so the copied text is the *current* value.

### 4.3 `ReadoutCard(title, subtitle="")`
- `QFrame#Card` (or a borderless "pane" variant for strips): title row (title, optional subtitle/badge, stretch, `CopyButton`), then a body.
- Body helpers: `add_kv(key, value_widget)` key/value grid (keys muted 12 px; values mono, selectable), `add_table(...)`, `add_bar(fraction, colour)`.
- `copy_text()` returns plain text in the formats of §7; the card's `CopyButton` uses it.

### 4.4 `ReadoutStrip(title)`
- A collapsible horizontal strip placed **under an image**: header row = chevron + UPPERCASE title + one-line **summary** (shown only when collapsed) + "Copy all".
- Body: a flow of `ReadoutCard`s in pane style, separated by 1 px vertical dividers. If the width is too small they wrap to a second line (use a simple flow layout or a `QGridLayout` re-flowed on resize).
- Collapsing it gives the space back to the image.

### 4.5 `PixelImageView`
Used by Image & calibration and Label frames. The camera frames are **160×120**.
- Paints the frame with **nearest-neighbour** scaling (`Qt.FastTransformation`, no smoothing), keeping the 4:3 aspect, centred.
- **Zoom:** `Fit` (largest size that fits; prefer the largest integer factor if it fills ≥ 90 % of the space), `2×`, `3×`, `4×` relative to Fit, plus Ctrl+wheel zoom around the cursor. Above Fit the view sits in a `QScrollArea` and can be panned (middle-drag or Space+drag).
- Overlays painted on top in image coordinates: matching-pixel tint, label paint (with opacity), PC detection boxes, region box (draggable/resizable, cyan `accent`), centre cross, hovered-pixel outline.
- **One** transform object (`widget↔image` coordinates) used by hover readout, region dragging and painting, so zoom/pan never breaks them.
- Small overlay chip bottom-left: `160×120 · fit` / current tool.
- Signals: `hovered(x, y)`, `left()`, `regionChanged(rect)`, `paintStroke(...)` (tab-specific).

### 4.6 Small pieces
- `StatusPill(tone, text)` — rounded chip with a coloured dot (ok/warn/err/neutral).
- `Banner(tone, text)` — inline info/warn/ok message with icon; text selectable; full text in tooltip if elided.
- `SegmentedButtons(options)` — exclusive `QButtonGroup` of checkable buttons drawn as one segmented control (zoom levels, label tools, shapes, filters). Each option can show a key hint in faint mono (e.g. `Balloon 1`).
- `ToggleSwitch` — checkable button drawn as a small switch (Live, Collect from camera, Record).
- `SplitterHandle` styling with a dotted grip.

---

## 5. Application shell (all tabs)

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ target_cam  Port [/dev/ttyACM0 ▾] Baud [460800 · frame 0.83 s ▾] [Set baud]   │ ← connection bar
│             [Connect|Disconnect]  (● Connected · /dev/ttyACM0 · 460800 baud)  │
├──────────────────────────────────────────────────────────────────────────────┤
│ Image & calibration │ Flight data │ Label frames │ Blobs                       │ ← QTabWidget
├──────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│                         (tab content, see §6)                                │
│                                                                              │
├──────────────────────────────────────────────────────────────────────────────┤
│ ▸ Console  <last line when collapsed>        ☐ CSV rows  [Copy] [Clear]      │ ← bottom dock
│ > !collect 0                                                                 │
│ # collect off                                                                │
│ > [camera command, e.g.  ?  m  H350  p red  !save               ] [Send]     │ ← ALWAYS visible
├──────────────────────────────────────────────────────────────────────────────┤
│ baud 460800  kB/s 1.8  rows/s 12.0  frames ok/bad 11/0  blobs ok/bad 545/0   │ ← status bar
│ junk 1  NAK 0  retries 1  timeouts 1                                    [⧉]  │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 5.1 Connection bar (top, `QToolBar` or `QWidget` in the main window, not in a tab)
- `target_cam` wordmark (mono, 14 px).
- Port `QComboBox` (editable; refreshed from `serial.tools.list_ports` when opened). This control and Connect/Disconnect are new: the old viewer only took the port on the command line. Full requirements in §13.1.
- Baud `QComboBox` showing the frame time, e.g. `115200 · frame 3.3 s`, `460800 · frame 0.83 s` (the existing values/behaviour of the old "Baud … Set" control on tab 1), and **Set baud** — moved here from tab 1's "Image link" group.
- **Connect / Disconnect** (primary when disconnected).
- `StatusPill`: err `Port not found: /dev/ttyACM0` · ok `Connected · <port> · <baud> baud` · warn while connecting. The full error text also goes to the console (it is currently truncated in the status bar).
- On the Label frames tab, when disconnected, a muted note: "Labelling works offline".

### 5.2 Tabs
Short names, long descriptions as tooltips:
- `Image & calibration`
- `Flight data` — tooltip "what the ESP32-S3 gets"
- `Label frames`
- `Blobs` — tooltip "classifier data"

### 5.3 Console (bottom `QDockWidget`, outside the tab widget)
- Header row: collapse chevron + "Console"; when collapsed, the **last console line** is shown (elided, selectable); `☐ CSV rows` (was "Show CSV rows" — same behaviour); **Copy** (whole log); **Clear**.
- Log: read-only `QPlainTextEdit`, mono 12 px, `setMaximumBlockCount(5000)`. Colours: sent commands (`> …`) `text`; camera replies (`# …`) `text_muted`; viewer errors `err_text`.
- **Command line + Send are outside the collapsible part — always visible on every tab.** Enter sends. Up/Down recall previous commands (small addition; skip if it conflicts with existing behaviour).
- The dock is vertically resizable (it's a dock); collapsed height ≈ header + command line only.

### 5.4 Status bar
- The existing counters, rendered as `key value` segments in mono 11.5 px: `baud`, `kB/s`, `rows/s`, `frames ok/bad`, `blobs ok/bad`, `junk`, `NAK`, `retries`, `timeouts`.
- Values > 0 for junk / NAK / retries / timeouts / bad are shown in `warn_text`.
- Selectable, horizontally scrollable if too long (never truncated), plus a copy button that copies `baud 460800 | kB/s 1.8 | …`.
- Connection errors are no longer shown here (they go to the pill and the console).

---

## 6. Tabs

### 6.1 Image & calibration

```
┌ toolbar ───────────────────────────────────────────────────────────────┐ ┌ Calibration ── Expand all  Collapse all ┐
│ [Grab frame] (Live ◯) │ Overlay [matching pixels tinted ▾] ☑ PC         │ │ ▸ Region           hue 341 ±27 · 42 %  │
│ detection boxes                       [Fit|2×|3×|4×] [Save frame…]      │ │ ▸ Colour test  stage 1 · colour table… │
├─────────────────────────────────────────────────────────────────────────┤ │ ▸ Sensor           AWB off · big-endian │
│                                                                         │ │ ▸ Camera flash memory          ● saved │
│                 PixelImageView (stretch, full pane width)               │ │                                        │
│                                                                         │ │   (QScrollArea, 400 px default)        │
├─────────────────────────────────────────────────────────────────────────┤ │                                        │
│ ▾ CAMERA READOUTS  <summary when collapsed>                [Copy all]   │ │                                        │
│ Pixel │ Detection (PC vs camera) │ Frame & link │ Camera settings       │ │                                        │
└─────────────────────────────────────────────────────────────────────────┘ └────────────────────────────────────────┘
                         ← horizontal QSplitter between image pane and inspector →
```

**Toolbar (above the image):** Grab frame (primary, shortcut G), Live toggle (a frame every loop), Overlay combo, ☑ PC detection boxes, zoom segmented control, Save frame….

**Camera readouts strip** (`ReadoutStrip`, open by default). Four cards, each with a copy button:
1. **Pixel under cursor** — colour swatch of the pixel, `x 157 · y 94`, chip `no match` / `match`; rows `rgb565 10e3`, `RGB 16 28 24`, `HSV H 160 S 109 V 28`. When not hovering: "hover over the image for a pixel's colour".
2. **Detection** — header badge `✓ PC = camera` when both rows agree (warn badge otherwise). Table rows **PC** (tooltip "PC (controls)") and **Camera** (tooltip "camera (its row)"), columns verdict chip (`TARGET` …), blobs, area, matched.
3. **Frame & link** — two-column key/value grid: seq, camera t, format, received (x s ago), on the wire (`460800 baud · 1.8 kB/s`), frames (`ok 11 · bad 0 (CRC/trunc.)`), last (x s ago). Warn note: "While frames are sent, the camera's dt_ms / FPS are not flight timing."
4. **Camera settings** — ok/warn line "Saved in flash — a power cycle keeps them" (replaces the old green bottom banner), detector (stage), preset, byte order.

Collapsed summary example: `x 157 y 94 · H 160 S 109 V 28 · TARGET 3 blobs, PC = camera · seq 543 · 1.8 kB/s · saved in flash`.

**Inspector sections** (`CollapsibleSection`s in a scroll area; replace the old right-edge Region/Colour/Blobs/Sensor/Flash toggle buttons):
- **Region** — `Use region as target` + hint "drag / resize the cyan box". A copyable **camera sampler** block: box `16×16 at (72, 52)`, hue `341 ± 27`, sat p10/50/90 `61 / 115 / 164`, val p10/50/90 `49 / 74 / 115`, matching `107 / 256 px (42%)` with a small bar. Below it the **hue histogram** (existing plot restyled: bars coloured by their hue, dashed markers at the PC hue window limits, axis 0–360, caption "Hue of region pixels with sat ≥ 30").
- **Colour test** — Detector stage combo + Set; Preset combo + Apply (+ `camera: red` echo). When the stage is not 0, show an **info banner**: "Colour table 1792216162 decides the pixels. Hue, tolerance, sat, value and c have no effect until stage 0." and render the HSV controls at reduced emphasis (still editable). A hue spectrum strip with the current window drawn as a white outlined band (handle wrap-around at 0/360). Four rows — Hue, Tolerance, Min sat, Min value — each: label, slider, spin box with unit, and a **camera** column chip: `✓ applied` (ok), the camera's differing value (warn), or `—` with tooltip "no camera". `Calibrate on centre (c)`.
- **Blobs & geometry** — the old "Blobs and geometry" group, which this spec originally left out: `Min area` (slider, px), `Target size` (spin only, mm), `h-FOV` (spin only, deg, 1 decimal), with the same camera column as Colour test. Header summary `area ≥ 25 px · 550 mm · FOV 54.0°`.
- **Sensor** — ☐ Auto white balance; `Byte order: big-endian` button.
- **Camera flash memory** — header summary `● saved` / `● unsaved`; buttons Save to camera, Reload saved, Factory defaults… (danger style; it already asks for confirmation — keep that dialog).

Removed: the "Image link" and "Files" groups (baud moved to the connection bar, link stats into the Frame & link card, Save frame into the toolbar).

### 6.2 Flight data

```
┌ toolbar ──────────────────────────────────────────────────────────────────────────────────────────┐
│ [✓ Flight-identical  image off · 115200 baud · stream on — rows are exactly what the ESP32-S3 …]   │
│                                              (● Record)  rows → target_cam/data/  [Reset counts]   │
├──────────────────────────────┬────────────────────────────────────────────────────────────────────┤
│ FLIGHT READOUTS   [Copy all] │ Field of view  (az right, el up · deg · white box = camera FOV)      │
│ ┌ TARGET  1 blob in frame ─┐ │                                                                    │
│ │ Blobs            [⧉ CSV] │ │            FOV plot (stretch)                                       │
│ │ #  az°  el°  range  notes│ │                                                                    │
│ └──────────────────────────┘ ├──────────────────── vertical splitter ───────────────────────────────┤
│ ┌ Blob 1 ────────────── ⧉ ┐  │ Blob 1 over time · camera clock · gaps = clipped/off   Window [30 s]│
│ │ area 860 px  box 42×54   │ │ az    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~  1.16 deg │
│ │ fill 0.38  centroid …    │ │ el    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~  3.35 deg │
│ └──────────────────────────┘ │ range ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~  1599 mm  │
│ ┌ Link health ─────────── ⧉ ┐ │       -30 s  -25  -20  -15  -10  -5  0                            │
│ │ stage 1 running · …      │ ├────────────────────────────────────────────────────────────────────┤
│ │ metric  typical   max    │ │ Last raw row  35993,1,83.17,50.81,1.16,3.35,860,…            [⧉] │
│ └──────────────────────────┘ │                                                                    │
└──────── scroll, 440 px ──────┴────────────────────────────────────────────────────────────────────┘
```

- **Toolbar:** the flight-identical message becomes a one-line `Banner` — ok tone `✓ Flight-identical …` when identical, warn tone `NOT flight-identical: no #!CFG from the camera yet; commands still in flight` otherwise. Full text in tooltip and selectable. Then Record (toggle; red dot; "Recording…" while on), the path `target_cam/data/` (selectable), Reset counts.
- **Left column** (scroll area, horizontal splitter with the plots):
  - **Verdict tile** — big `TARGET` on `verdict_target` (white, 26 px bold), plus "N blob(s) in frame · largest first". Other states keep their current wording (e.g. `NO DATA` on a neutral grey tile).
  - **Blobs table** — read-only `QTableWidget`, rows 1–4 (row 1 "1 largest" highlighted), columns az °, el °, range mm, notes; empty rows show `—` in faint text. Copy → CSV (§7).
  - **Blob 1** card — area, box, fill, centroid, matched px; subtitle "N in frame · 4 reported at most".
  - **Link health** card — first a line `stage 1 running · requested 1 · lut ok · clf none · cnn none · csv v2`; then a 3-column table `metric | typical | max` with a small note line under a value where the old text had one:
    `rows/s 12.5 (camera fps setting 12.5)`, `dt_ms mean 80.0 | 80.0 (jitter (sd) 0.0)`, `dt≠80 4 of 320 rows | 3353.0 (worst gap)` **in warn colour when > 0**, `proc_us`, `t_px_us`, `t_b_us`, `t_c_us`, `loop_us p50 | max` with a bar for max/budget (80000 us), `ok 100.0 % (of rows in the last 5 s)`, `wire 1743 B/s` with a bar for % of capacity. Copy → the original plain-text block (§7).
- **Right side** (vertical splitter): **FOV plot** (existing plot, restyled: `plot_bg`, minor/major grid, zero axes, white camera-FOV rectangle, blobs as filled circles numbered, crosshair at blob 1); **time series** — three stacked strips sharing one x-axis (camera seconds, −30…0), each with min/max labels at left, its name in the series colour, and the **current value** big at the right (`1.16` deg, `3.35` deg, `1599` mm); a Window combo (10/30/60 s) if the plot code supports it, otherwise omit; **Last raw row** — read-only mono line with horizontal scroll + copy.

### 6.3 Label frames

```
┌ toolbar ───────────────────────────────────────────────────────────────┐ ┌ Labelling ─ Expand all  Collapse all ┐
│ [◀] bal_green_150cm_wood          [▶]  (● saved)      [Fit|2×|3×|4×]    │ │ ▸ Frames        73 · all labelled    │
│     data/ · frame 1 of 73                                               │ │   …/projects/target_cam/data [Open…] │
├─────────────────────────────────────────────────────────────────────────┤ │   [Rescan]                           │
│                                                                         │ │   🔍 [filter, e.g. red_s1 or 150cm ] │
│              PixelImageView (as large as possible, centred)             │ │   ✓ bal_green_150cm_wood             │
│                                                                         │ │   ✓ orange_s3/bal_red_155cm_…        │
│                                                                         │ │ ▸ Paint     balloon · ellipse · r 2  │
├─────────────────────────────────────────────────────────────────────────┤ │   Label  [Balloon 1|Background 2|Erase 0]│
│ ▾ LABEL STATS  <summary when collapsed>                     [Copy all]  │ │   Shape  [Brush B|Rectangle R|Ellipse E]│
│ Pixel │ Labels (bar + counts) │ Detector vs labels (TP/FN/FP/split, P/R)│ │   Brush radius [2 px]                │
└─────────────────────────────────────────────────────────────────────────┘ │   [Rest → background] band [2 px]     │
                                                                            │   [Undo] [Clear all…] [Save]          │
                                                                            │ ▸ Show      labels 70 % · boxes on   │
                                                                            └──────────────────────────────────────┘
```

Priority on this tab: **the biggest possible image**, because labelling needs zoom.
- **Toolbar:** previous/next buttons around the current frame name (mono) with a second line `<subfolder> · frame i of N`; saved/unsaved chip; zoom.
- **Image:** full width of the pane, centred; when zoomed it scrolls/pans. The overlay chip shows the active tool, e.g. `balloon · ellipse`.
- **Label stats strip** (`ReadoutStrip`, collapsible):
  1. **Pixel** — hover readout (or "hover over the image").
  2. **Labels** — subtitle `160 × 120 = 19200 px`; stacked bar balloon / background / unlabelled; rows `balloon 2187 px 11.4 %`, `background 17013 px 88.6 %`, `unlabelled 0 px 0 %` with colour swatches; `balloons (labelled objects) 1` and `#1 · 2187 px at (75, 62)`.
  3. **Detector vs labels** — a line `stage 0 · HSV window` / `hue 145 ±40 · sat ≥ 48 · val ≥ 82` / `1 blob reported · blob 1 = balloon`; four count tiles **TP** (ok tone when > 0), **FN**, **FP**, **split**; precision and recall as bars with values; caption "pixel level, detector vs your paint".
  - Collapsed summary: `balloon 2187 px · background 17013 px · unlabelled 0 · TP 1 FN 0 FP 0 split 0 · precision 84.6 % · recall 81.9 %`.
- **Inspector:**
  - **Frames** — folder path elided on the left (full path in tooltip, selectable), Open…, Rescan; a **filter** line edit (case-insensitive substring); the frame list (`QListWidget`): green ✓ for labelled frames, subfolder prefix in faint text, file name in normal text; selection drives the image. Header summary `N · all labelled` / `N · k unlabelled`.
  - **Paint** — Label segmented `Balloon 1` (swatch `label_balloon`) / `Background 2` (swatch `label_not`) / `Erase 0`; Shape segmented `Brush B` / `Rectangle R` / `Ellipse E`; Brush radius spin (px); `Rest → background` + band spin (px); Undo / Clear all… (danger) / Save (primary). Header summary `balloon · ellipse · r 2 px`.
  - **Show** — Label opacity slider + `%`; ☑ Detector boxes (subtitle "the camera's settings for this frame"); Detector stage combo.
- **Shortcuts** (active only when focus is not in a text field): `1` `2` `0` label, `B` `R` `E` shape, `←`/`→` previous/next frame, `Ctrl+Z` undo, `Ctrl+S` save.

### 6.4 Blobs (classifier dataset — 12 features per blob for training a small NN)

```
┌ dataset toolbar ─────────────────────────────────────────────────────────────────────────────────────┐
│ (◯ Collect from camera) camera: not collecting │ [Add from labelled frames…] [Load dataset…]          │
│ [Save dataset…] [Clear…]                         76 blobs  0 balloon · 0 not · 76 ?  ▬▬▬▬  (unsaved) │
├───────────────────────────────────────────────────────────────────┬──────────────────────────────────┤
│ (○ board = PC: not checked) Collect, then send g: the frame comes │ Blob 18 of 76          [Copy all]│
│ with its records and the PC recomputes them on the same pixels.   │ ┌──────┐ Record             [⧉]  │
│ [All 76|Balloon 0|Not 0|? 76] │ 1 selected [Balloon B][Not a      │ │ crop │ source live t26622234     │
│ balloon N][Unlabel U]                              Size ──●──      │ │144px │ blob 1 · stage 1 · conf -1 │
├───────────────────────────────────────────────────────────────────┤ └──────┘ fw c2ba88a-dirty · fv 1  │
│ ▣ ▣ ▣ ▣ ▣ ▣ ▣ ▣ ▣ ▣ ▣ ▣                                          │ centroid · area · box · crop      │
│ ?  ?  ?  ?  ?  ?  ?  ?  ?  ?  ?  ?     (thumbnail grid, scrolls)  │ Label [Balloon B|Not N|? U]       │
│ ▣ ▣ ▣ …                                                          │ Feature vector · 12 inputs   [⧉] │
│                                                                   │  0 fill        0.2045             │
│ hint: click selects, shift-click adds, double-click cycles…       │  …                                │
└───────────────────────────────────────────────────────────────────┴──────────────────────────────────┘
```

- **Dataset toolbar:** `ToggleSwitch` "Collect from camera" + state text (`camera: collecting` ok / `camera: not collecting` muted) — same behaviour as the current "Collect from camera" / "camera: …" pair; Add from labelled frames…; Load dataset…; **Save dataset… (primary)**; Clear… (danger). Right side: `N blobs`, `b balloon · n not · u ?`, a stacked bar (balloon `label_balloon`, not `label_not`, rest grey), and an `unsaved` warn chip when there are unsaved changes.
- **Check line:** chip `board = PC: not checked` (neutral) / `board = PC ✓` (ok) / mismatch (warn), followed by the hint text.
- **Labelling toolbar:** filter segmented `All / Balloon / Not / ?` with live counts; `N selected`; **Balloon B**, **Not a balloon N**, **Unlabel U** buttons acting on the selection (same as the existing B/N/U keys); thumbnail **Size** slider (48–128 px, default 72).
- **Grid:** `QListView` in `IconMode`, `ExtendedSelection`, uniform item sizes, `Adjust` resize mode, with a custom `QStyledItemDelegate` that draws the 24×24 crop (`BLOB_CROP`; fixed by what the ESP32-CAM can send — do not change it) scaled with nearest-neighbour, a 1 px border (2 px `accent` outline when selected), and a **label badge** under it: `balloon` (accent fill), `not` (`label_not` fill), `?` (muted text). Double-click cycles `? → balloon → not → ?`. Keep the existing model/data; only the view and delegate change.
- **Selected blob inspector** (scroll area):
  - Title `Blob k of N` + Copy all. If several are selected: info banner "N blobs selected — the label buttons apply to all of them. Showing the last one clicked."
  - 144×144 nearest-neighbour **crop preview** + **Record** key/value block (source `live t26622234`, blob, stage · conf, fw, fv, centroid, area, box, crop) with copy.
  - **Label** segmented `Balloon B / Not N / ? U` for this blob.
  - **Feature vector** — read-only `QTableWidget` with columns `#`, `feature`, `value` (mono, right-aligned) for the 12 features in this exact order: `fill, log_aspect, hue_spread, sat_mean, sat_std, val_mean, highlight, edge, texture, ring, cy_norm, log_area`. Copy → CSV header + row (§7). Cells selectable (Ctrl+C copies the selected cells as TSV).

---

## 7. Copy formats

Copy buttons must produce plain text that is useful when pasted into notes, a spreadsheet, or a training script. Keep the **existing wording** where the old UI already printed text, so nothing downstream breaks.

| Source | Copied text |
|---|---|
| Pixel | `x 157  y 94  rgb565 10e3  RGB 16 28 24 -> H 160 S 109 V 28  no match` |
| Detection (tab 1) | `PC (controls): TARGET  blobs 3  area 374  matched 653` ⏎ `camera (its row): TARGET  blobs 3  area 374  matched 653` |
| Frame & link | `frame seq 543, camera t 139.6 s, rgb565be; received 1.2 s ago` ⏎ `460800 baud, 1.8 kB/s on the wire. frames ok 11, bad 0 (CRC/truncated), last 488.5 s ago` |
| Camera settings | `Camera settings: SAVED in flash` ⏎ `detector stage 1 · colour table; preset red; byte order big-endian` |
| Region sampler | `16x16 at (72,52): hue 341 +/-27 (camera's sampler)` ⏎ `sat p10/50/90 61/115/164  val p10/50/90 49/74/115` ⏎ `matching now: 107 of 256 px (42%)` |
| Blobs table | CSV: `#,az_deg,el_deg,range_mm,notes` ⏎ `1,1.16,3.35,1599,` |
| Blob 1 | `blob 1: area 860 px  box 42x54  fill 0.38  centroid (83.2, 50.8)  matched px 934` ⏎ `blobs in frame 1 (4 reported at most)` |
| Link health | the original multi-line block (`stage …`, `rows/s …`, …, `wire …`) exactly as the old label printed it |
| Last raw row | the raw CSV line verbatim |
| Label stats | `<frame name>  saved` ⏎ `balloon 2187 px  background 17013 px  unlabelled 0 px (0 %)` ⏎ `balloons (labelled objects): 1  [2187 px at 75,62]` |
| Detector vs labels | `detector, stage 0 (hue 145 +/-40, sat >= 48, val >= 82): 1 blob(s) reported` ⏎ `vs labels: TP 1  FN 0  FP 0  split 0  blob 1 = balloon` ⏎ `pixels: precision 84.6 %  recall 81.9 %` |
| Board = PC check (Blobs) | `board = PC ✓` ⏎ `12 records checked against the PC library: features equal in 12, crops identical in 12; worst feature difference 0 (relative)` |
| Blob record | the old right-panel text block (`live t26622234  blob 1`, `label ?`, `stage 1  conf -1  fw c2ba88a-dirty`, `fv 1`, `centroid …  area …`, `box …`, `crop …`) |
| Feature vector | CSV: `fill,log_aspect,hue_spread,sat_mean,sat_std,val_mean,highlight,edge,texture,ring,cy_norm,log_area` ⏎ `0.2045,0.2113,11.82,163.5,20.09,63.68,0,18.58,24.8,0.06548,0.4295,4.29` |
| Status bar | `baud 460800 | kB/s 1.8 | rows/s 12.0 | frames ok/bad 11 / 0 | …` |
| Console | the whole log text |
| "Copy all" | the group's cards joined with blank lines |

(Numbers above are examples taken from real sessions; always copy the live values.)

---

## 8. Keyboard shortcuts

| Key | Where | Action |
|---|---|---|
| `G` | Image & calibration | Grab frame |
| `C` | Image & calibration | Calibrate on centre (sends the existing `c`) |
| `Ctrl+L` | anywhere | focus the command line |
| `1` / `2` / `0` | Label frames | Balloon / Background / Erase |
| `B` / `R` / `E` | Label frames | Brush / Rectangle / Ellipse |
| `←` / `→`, `PgUp` / `PgDn` | Label frames | previous / next frame (PgUp/PgDn are the existing keys; keep them) |
| `Ctrl+Z`, `Ctrl+S` | Label frames | undo, save |
| `B` / `N` / `U` | Blobs | label selection balloon / not / unlabelled (existing) |

Register them with `QShortcut` on the tab widget using `Qt.WidgetWithChildrenShortcut`, and ignore them while a `QLineEdit`/`QSpinBox` has focus, so typing commands never triggers them. Show the key next to the matching button (faint mono).

---

## 9. Persistence

Use the store the viewer already has, `QSettings("icarus_blimp", "tcview")` (§13), to remember: the last port and baud (§13.1), window geometry/state, current tab, every splitter's sizes, each collapsible section's open state, readout strips open/closed, console open/closed and height, zoom level per image view, Label frames folder, label opacity, Blobs thumbnail size. Restore on start; fall back to sensible defaults if values are missing or invalid (e.g. a saved geometry larger than the current screen).

---

## 10. Implementation plan (one phase at a time; stop after each)

**Phase 0 — Reconnaissance (no code changes).**
Read the code and report: Qt binding and version, plotting library, file/module layout, where each tab is built, the widgets that show each piece of text listed in §6/§7, where serial I/O and the CSV/record parsing live, and any fixed sizes. Produce a mapping table *old widget → new widget/location*. Confirm with the user.

**Phase 1 — Theme and shared widgets.**
`tcview/ui/theme.py` (tokens, palette, QSS, font picking) and `tcview/ui/widgets/` (§4). Apply the theme to the existing UI without moving anything. A tiny demo script that shows each widget is welcome. Screenshots → confirm.

**Phase 2 — Application shell.**
Connection bar (move port/baud/connect controls here) with the connection management of §13.1, `QTabWidget` with the new names/tooltips, console as bottom dock with an always-visible command line, new status bar. Old tab contents are placed in the tabs unchanged. Verify at 1280×720 that the command line and status bar are visible on all four tabs. Screenshots → confirm.

**Phase 3 — Image & calibration** (§6.1): `PixelImageView` with zoom, toolbar, Camera readouts strip, collapsible inspector, remove the right-edge toggle buttons and old Image link/Files groups. Check hover readout and region drag at every zoom. Screenshots → confirm.

**Phase 4 — Flight data** (§6.2). Screenshots → confirm.

**Phase 5 — Label frames** (§6.3): reuse `PixelImageView`; painting must hit the right pixels at every zoom/pan. Screenshots → confirm.

**Phase 6 — Blobs** (§6.4): list view + delegate, selection/labelling, inspector, feature table. Screenshots → confirm.

**Phase 7 — Shortcuts, persistence, polish** (§8, §9), then run the full checklist in §11.

---

## 11. Verification checklist

Take screenshots offscreen so they can be checked without a display:

```bash
QT_QPA_PLATFORM=offscreen python -m <app module> --screenshot-test   # or a small script that builds MainWindow, resizes, and calls window.grab().save(...)
```

For each tab, capture at **1920×1080, 1440×900, 1280×720** (and 1024×600 for a smoke test), with the console open and collapsed. Then check:

- [ ] Nothing is clipped; command line + Send and the status bar are visible on every tab at 1280×720.
- [ ] Inspectors scroll instead of clipping; splitters move and are remembered.
- [ ] The 160×120 image is sharp (nearest-neighbour) at Fit and at 2×/3×/4×; panning works; hover pixel readout, region drag and painting land on the correct pixel at every zoom.
- [ ] Every readout listed in §7 is selectable and its copy button puts the expected text on the clipboard (test with `QApplication.clipboard().text()`).
- [ ] Collapsing a readout strip gives its space to the image, and the summary line shows the key values.
- [ ] Colour test: banner appears and HSV controls are de-emphasised when stage ≠ 0; camera column shows applied / differing / no camera correctly.
- [ ] Status-bar counters turn amber when > 0 (junk, NAK, retries, timeouts, bad).
- [ ] Shortcuts work, and do nothing while typing in the command line.
- [ ] With the camera connected: Grab, Live, all commands, Record, Collect, Save/Load dataset behave exactly as before (compare CSV/record files against the old version for the same session if possible).
- [ ] With no camera: the app starts, shows `Port not found` in the pill and the full error in the console; Label frames and Blobs (Load dataset) still work offline.

---

## 12. Out of scope

- Any change to the firmware, the serial protocol, the commands, the CSV columns, the dataset/label file formats, or the detection/feature maths.
- Training the neural network (this UI only collects and labels the 12-feature dataset).
- New features beyond what is described here (ask the user first).

---

## 13. Phase 0 findings and decisions (2026-10-17)

Reconnaissance was done on branch `target-cam-learned-detector` at `71fd665`. Where this section and the sections above disagree, this section wins.

### 13.0 What the code is (measured)

- PyQt6 6.11.0 (Qt 6.11.0) and pyqtgraph 0.14.0 in the `targetcam` conda env. IBM Plex Sans and IBM Plex Mono are installed on the dev machine. The env has numpy 2.2.6 although `requirements.txt` pins 1.26.4.
- Package `firmware/projects/target_cam/host/viewer/tcview/`: `__main__.py` (MainWindow, console, status bar, `LabelWindow` for `--label`), one file per tab (`calib_tab.py`, `flight_tab.py`, `label_tab.py`, `blobs_tab.py`), `widgets.py` (`ParamControl`, `HueBar`, `chip()` and the GREEN/AMBER/RED/GREY colours). Serial I/O is `link.py` (`SerialLink`, a QThread); stream parsing is `protocol.py` (`StreamParser`). `MainWindow._event` dispatches events to the tabs.
- Baseline: `python -m pytest -q` → 99 passed.
- Why the window clips: a `QTabWidget`'s minimum size is the largest of its pages' minimums. The fixed 644×484 image views, the 150 px flight table, the 330 px plot minimum and the 380×300 FOV plot hold the whole window at ≈ 980 px tall (1012 px on Flight data) on **every** tab, so at 1280×720 the bottom is cut off.
- The tests read widget text: `flight.banner` (starts with `FLIGHT-IDENTICAL`), `flight.health` (`rows/s`, `dt != 80`, `stage      1 running`), `calib.stage_lbl` (`requested, running 1`), `label.det_lbl` (`TP 1  FN 0  FP 0`), and Label list items starting with `[x]`. They also use the attributes `win.send`, `win.link`, `win.connect_link`, `win.tabs`, `win.calib/flight/blobs`, and many `LabelTab` members (`lab`, `step`, `sf`, `dab`, `fill_shape`, `fill_rest`, `do_undo`, `radius`, `listw`, `dirty`, `clear`, `stems`). When the visible text changes, the tests switch to the widget's copy text (§7 keeps the old wording there), and the attribute names stay.

### 13.1 Connection management (new — requested by the user)

The viewer took its port only from `--port`. That works for one developer and not for the other people who will use it, so the port is chosen in the GUI. Everything below is needed for this to work properly, and is part of Phase 2.

- **Port list.** `serial.tools.list_ports.comports()`, shown as `<device> — <description>` (the device path is the item data). USB ports first; legacy `ttyS*` ports without a USB descriptor are hidden. The list is refreshed whenever the combo opens and whenever the link drops. The combo is editable, so a path that isn't listed (`/dev/serial/by-id/…`, a pty) can be typed.
- **Which port is selected at start.** `--port` if given; otherwise the last port that connected successfully (QSettings), if it is present; otherwise the first USB port. With `--fake`, the fake camera's pty is added as `FAKE camera (<pty>)` and selected.
- **Auto-connect** only when `--port` or `--fake` is given, which keeps today's behaviour. With no arguments the window now opens **disconnected** instead of exiting with a usage error. `--label` is unchanged (§13.3).
- **Connect.** It creates and starts a `SerialLink` for the chosen port. The pill shows warn `Connecting…` until the port is open, then ok `Connected · <port> · <baud> baud`, and `k` is sent as today. The port and baud combos are disabled while connected (you have to disconnect to change port).
- **SerialLink reports structured state.** Add signals for opened / closed(reason) next to the existing human-readable `state` text, so the GUI never has to parse strings.
- **Open errors.** The pill shows a short err message and the console gets the full exception. Known causes get a hint: permission denied → "add your user to the `dialout` group, then log out and in"; busy/locked → "the port is in use by another program (another viewer, `idf.py monitor`, a serial terminal)"; not found → "unplugged, or a different port name".
- **One viewer per port.** Open the port with pyserial's `exclusive=True`, so a second viewer, or a second person on a shared machine, gets the "in use" error instead of silently splitting the byte stream with the first one.
- **Disconnect.** If the link is not at 115200, first ask the camera back down (`B115200`, wait for the handshake, bounded to about 5 s), then stop the link and close the port. Closing the window does the same. Otherwise a camera left at 460800 sits there after the viewer has gone, and the next connection (always at 115200) hears only junk. This problem already exists today when the viewer is closed at a fast baud, and the camera's own revert does not cover it. Nothing else about the camera's state is changed on disconnect.
- **Link lost** (unplugged, brownout, `link lost: …`). The GUI goes to the disconnected state with an err pill `Link lost · <port>`, re-enables Connect and refreshes the port list. It does **not** reconnect by itself: a silent reconnect could take a port someone else has just opened.
- **While disconnected.**
  - The command line stays enabled. A command typed while disconnected is not sent, and the console says `# [viewer] not connected: '<cmd>' not sent`.
  - Each tab gets an `on_disconnected()` that forgets the camera state: `cfg = {}`, every `ParamControl` goes back to "no camera", the flight banner reads "no #!CFG", and Blobs shows `camera: unknown`.
  - A Flight recording in progress is stopped and its file closed (and logged), so a file never spans two connections.
  - Frames, labels and the blob dataset already on screen stay.
- **Counters** belong to a link. A new connection starts them at zero, and the status bar shows `—` while disconnected.
- The window title follows the connected port. The port that connected last is saved in QSettings.
- **Tests** (extend `test_gui_fake.py`): connect to the fake through the GUI controls; disconnect and reconnect to the same fake; disconnect at 460800 leaves the fake at 115200; a nonexistent port gives the err pill and the console line; a second link on an open port gets "in use".

### 13.2 `PixelImageView` keeps the scroll bars

§4.5 stands as written. Above Fit, the view sits in a `QScrollArea` with real scroll bars, because small laptop screens need them. Drag/Space-panning is in addition to the scroll bars, not instead of them. Whether it is built on a pyqtgraph `ViewBox` sized to the zoom inside the scroll area, or on a hand-painted `QWidget`, is an implementation choice for Phase 3. Either way, one transform serves hover, region drag and painting.

### 13.3 Other decisions

- Blob crops stay **24×24** (`BLOB_CROP`). That is a limit of what the ESP32-CAM sends. Thumbnails are 48–128 px (default 72 = ×3), and the inspector preview is 144 px (×6).
- Image & calibration gets a fifth inspector section, **Blobs & geometry** (§6.1), so Min area, Target size and h-FOV are not lost.
- `--label` keeps the standalone `LabelWindow` (it does not use the full shell running disconnected). It gets the theme and the new Label frames layout.
- Label frames: ←/→ added, PgUp/PgDn kept (§8).
- QSettings stays `("icarus_blimp", "tcview")`. The old `calib/show/*` keys become unused when the panel toggle strip goes.
- New modules go in `tcview/ui/theme.py` and `tcview/ui/widgets/`. `tcview/widgets.py` stays (`ParamControl`, `HueBar`), and its `chip()` colours map onto the theme tones.
- The Flight "Window 10/30/60 s" combo is omitted, because 60 s would need a longer history buffer.
- `ParamControl`'s camera column keeps all four states: `no camera`, `sending`, `✓ applied`, and the camera's differing value (was "not sent").
- The stage line keeps all five of its states. The mismatch states ("colour table MISMATCH", "PC has no colour table") use err tone, "requested N, running M" uses warn, stage ≥ 1 uses info, and stage 0 is neutral.
- The Save frame result (`file_lbl`, "saved X.{png,rgb565,json}") goes to the console.
