"""Shared widgets of the redesigned viewer (docs/GUI_REDESIGN.md §4)."""
from .controls import Segment, SegmentedButtons, ToggleSwitch
from .copy_button import CopyButton
from .pills import Banner, StatusPill
from .pixel_view import ZOOMS, PixelImageView
from .readout import (Bar, FlowLayout, ReadoutCard, ReadoutStrip, ReadoutTable,
                      StackedBar, value_label)
from .section import CollapsibleSection, ElidedLabel, HeaderButton
from .splitter import GripSplitter
from .strip_split import StripSplitter

__all__ = ["Banner", "Bar", "CollapsibleSection", "CopyButton", "ElidedLabel",
           "FlowLayout", "GripSplitter", "HeaderButton", "PixelImageView", "ReadoutCard",
           "ReadoutStrip", "ReadoutTable", "Segment", "SegmentedButtons", "StatusPill",
           "StackedBar", "StripSplitter", "ToggleSwitch", "value_label", "ZOOMS"]
