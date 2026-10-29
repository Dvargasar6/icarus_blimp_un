import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The viewer remembers the last port, baud and panel choices in
# QSettings("icarus_blimp", "tcview"). Tests get a store of their own, so they
# neither read the user's choices nor leave a fake camera's pty behind in them.
from PyQt6.QtCore import QSettings  # noqa: E402

_settings_dir = tempfile.mkdtemp(prefix="tcview-test-settings-")
atexit.register(shutil.rmtree, _settings_dir, True)
# QSettings(org, app) always uses NativeFormat, an .ini file on Linux.
QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, _settings_dir)

# One QApplication for the whole session, held here. Each module's `app`
# fixture finds it with QApplication.instance(); when a module-scoped fixture
# held the only reference, pytest dropped it at the module's end, and sip
# destroyed the QApplication with the module's closed (not yet collected)
# windows still alive. Qt then deleted those windows from C++, and sip never
# learned that their C++-made children - pyqtgraph's sub-menus, viewports,
# status bars - had died with them. Their wrappers stayed in sip's address
# map, where QWidget is also listed at +16 (its QPaintDevice part), so a later
# object allocated at that address came back as a dead wrapper pointing 16
# bytes too low: the intermittent segfault in pyqtgraph's menu set-up. The
# viewer itself makes one QApplication and keeps it until exit.
from PyQt6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_settings():
    """Every test starts from an empty store. A window saves its layout
    (§9) when it closes, and the next test's window would open on it: a
    different tab, the console folded, a remembered folder."""
    QSettings("icarus_blimp", "tcview").clear()
    yield
