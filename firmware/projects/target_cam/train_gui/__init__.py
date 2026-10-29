"""
Training and verification GUI for the stage 1 colour table:

    conda activate targetcam
    python -m train_gui            # from target_cam/

It is a front end to the two command-line tools, not a second implementation:
training runs host/train/train_lut.py as a subprocess (the exact command is
shown, so the terminal routine in the README stays learnable), and scoring
calls the same tcview.labels.score_frame that host/tools/eval_frames.py
calls, through the same shared library the camera's C is built from. A table
trained here is byte-identical to one trained from the terminal.

Tables go to target_cam/tables/<name>/lut_model.{bin,h,json}, one folder per
table, named tabla_<n>_r_<m>_o_<j>_p_<l> (see paths.table_name). The
firmware's own table (main/models/) is never written by this program; it only
appears as a choice to score and compare against.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET_CAM = HERE.parent
# The viewer package (theme, widgets, detector, labels) and the trainer are
# imported in place rather than installed, as eval_frames.py does.
for p in (TARGET_CAM / "host" / "viewer", TARGET_CAM / "host" / "train"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
