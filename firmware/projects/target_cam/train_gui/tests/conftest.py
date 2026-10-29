import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))     # target_cam/

import train_gui  # noqa: E402,F401  (adds host/viewer and host/train to sys.path)
from PyQt6.QtCore import QSettings  # noqa: E402

# A settings store of the tests' own, so they neither read the user's last
# choices nor overwrite them (same reason as the viewer's conftest).
_settings_dir = tempfile.mkdtemp(prefix="train-gui-test-settings-")
atexit.register(shutil.rmtree, _settings_dir, True)
QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, _settings_dir)
