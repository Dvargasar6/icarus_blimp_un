"""Phase D: model records over the link. The #!MODEL / #!UPLOAD protocol,
the helpers that keep the camera's models and plan an upload, and the
Firmware tab's backup, upload and unload against the fake camera on a pty."""
import shutil
import time
import zlib
from pathlib import Path

import pytest
from PyQt6.QtWidgets import QApplication

from tcview import detector as D
from tcview import install as I
from tcview import library as Lib
from tcview import protocol as P
from tcview.__main__ import MainWindow
from tcview.fake_cam import FakeCamera

MODELS = Lib.MODELS_DIR
CLF_SET = MODELS / "firmware_id1792216162"      # the firmware's table + a classifier


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def wait(app, cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


# ---- protocol ----------------------------------------------------------------------

def test_model_packet_and_replies_parse_at_any_chunking():
    rec = D.LUT_MODEL.read_bytes()
    wire = (b"#!ACK !dump lut\r\n" + P.encode_model(3, "lut", "build", rec) +
            b"#!MODEL none kind=clf src=none\r\n#!UPLOAD ready kind=clf len=180\r\n"
            b"#!UPLOAD refused kind=clf reason=crc\r\n")
    for step in (1, 7, 64, len(wire)):
        p = P.StreamParser()
        evs = []
        for i in range(0, len(wire), step):
            evs += p.feed(wire[i:i + step])
        m = [e for e in evs if isinstance(e, P.ModelRec)]
        assert len(m) == 1 and m[0].data == rec and (m[0].kind, m[0].src) == ("lut", "build")
        assert [e for e in evs if isinstance(e, P.ModelNone)] == [P.ModelNone("clf", "none")]
        up = [e for e in evs if isinstance(e, P.Upload)]
        assert [(u.status, u.v.get("reason")) for u in up] == [("ready", None), ("refused", "crc")]
        assert p.models_ok == 1 and p.junk_lines == 0
    # A damaged payload is dropped, never misread.
    bad = bytearray(P.encode_model(4, "clf", "upload", b"x" * 40))
    bad[60] ^= 1
    p = P.StreamParser()
    evs = p.feed(bytes(bad))
    assert not any(isinstance(e, P.ModelRec) for e in evs) and p.models_bad == 1


# ---- helpers -------------------------------------------------------------------------

def test_save_camera_models_keeps_new_bytes_once(tmp_path):
    recs = {"lut": D.LUT_MODEL.read_bytes(), "clf": (CLF_SET / "clf_model.bin").read_bytes()}
    name, new = I.save_camera_models(recs, {"lut": "build", "clf": "upload"}, tmp_path, now=1e9)
    assert new and name.startswith("camera_%d_" % D.models().lut_id)
    s = Lib.load_set(tmp_path / name)
    assert s.stages == [1, 2] and s.meta["clf"]["camera_src"] == "upload"
    assert I.check_installable(s) == ""                 # it can go back onto the camera
    assert I.save_camera_models(recs, {}, tmp_path) == (name, False)
    # Fewer records: another set.
    name2, new2 = I.save_camera_models({"lut": recs["lut"]}, {"lut": "build"}, tmp_path,
                                       now=1e9 + 1)
    assert new2 and name2 != name


def test_upload_plan_and_command():
    s = Lib.load_set(CLF_SET)
    plan = I.upload_plan(s, {"cnn_src": "upload", "clf_src": "build"})
    assert [p[:2] for p in plan] == [("upload", "lut"), ("upload", "clf"), ("unload", "cnn")]
    rec = plan[1][2]
    assert I.upload_command("clf", rec) == "!upload clf %d %08x" % (len(rec), zlib.crc32(rec))
    assert [p[:2] for p in I.upload_plan(s, {})] == [("upload", "lut"), ("upload", "clf")]


# ---- the Firmware tab against the fake camera ------------------------------------------

@pytest.fixture
def rig(app, tmp_path):
    cam = FakeCamera().start()
    win = MainWindow(cam.port, " test")
    models = tmp_path / "models"
    shutil.copytree(CLF_SET, models / CLF_SET.name)
    fwt = win.firmware
    fwt.models_dir = models
    fwt.confirm = lambda text: True
    fwt.reload_sets()
    win.show()
    win.connect_link()
    assert wait(app, lambda: fwt.cfg.get("upload") == 1), "no #!CFG with upload=1"
    yield win, cam, fwt, models
    win.close()
    cam.stop()


def test_backup_upload_and_unload(app, rig):
    win, cam, fwt, models = rig
    assert fwt.backup_btn.isEnabled() and not fwt.unload_btn.isEnabled()
    # Back up: the fake runs the build's table only.
    assert fwt.backup()
    assert wait(app, lambda: fwt.xfer is None, 15)
    saved = [d for d in models.iterdir() if d.name.startswith("camera_")]
    assert len(saved) == 1 and Lib.load_set(saved[0]).stages == [1]
    assert "kept as models/" in fwt.xfer_banner.text()
    # Again: the same bytes are not saved twice.
    fwt.backup()
    assert wait(app, lambda: fwt.xfer is None, 15)
    assert len([d for d in models.iterdir() if d.name.startswith("camera_")]) == 1
    assert "already saved" in fwt.xfer_banner.text()

    # Upload the set with the classifier: it runs from the upload partition.
    fwt.set_combo.setCurrentIndex(fwt.set_combo.findData(str(models / CLF_SET.name)))
    assert fwt.upload_btn.isEnabled() and fwt.upload()
    assert wait(app, lambda: fwt.xfer is None, 20), fwt.xfer_banner.text()
    assert fwt.xfer_banner.tone() == "ok", fwt.xfer_banner.text()
    assert cam.uploaded.keys() == {"lut", "clf"}
    assert fwt.cfg["clf_src"] == "upload" and fwt.cfg["clf"] == "ok"
    assert fwt.cfg["clf_id"] == Lib.load_set(CLF_SET).train_id("clf")
    assert fwt.unload_btn.isEnabled()

    # Back to the build's models.
    fwt.unload_btn.click()
    assert wait(app, lambda: fwt.cfg.get("clf_src") == "none", 10)
    assert not cam.uploaded


def test_a_refused_upload_stops_and_says_why(app, rig, monkeypatch):
    win, cam, fwt, models = rig
    fwt.set_combo.setCurrentIndex(fwt.set_combo.findData(str(models / CLF_SET.name)))
    real = I.upload_command
    # A wrong CRC in the command: the camera refuses the record after the bytes.
    monkeypatch.setattr(I, "upload_command",
                        lambda kind, rec: real(kind, rec)[:-8] + "00000000")
    assert fwt.upload()
    assert wait(app, lambda: fwt.xfer is None, 20)
    assert fwt.xfer_banner.tone() == "err" and "crc" in fwt.xfer_banner.text()
    assert not cam.uploaded


def test_flash_unloads_uploaded_models_first(app, rig, monkeypatch):
    """Uploaded models survive a reflash and would shadow the build's: Flash…
    sends !unload all and waits for the camera to confirm before it lets go
    of the port."""
    win, cam, fwt, models = rig
    cam.uploaded["clf"] = (CLF_SET / "clf_model.bin").read_bytes()
    win.send("k")
    assert wait(app, lambda: fwt.cfg.get("clf_src") == "upload")
    monkeypatch.setattr(fwt, "port", lambda: cam.port)
    monkeypatch.setattr(I, "build_has", lambda *a: True)
    fwt._refresh()
    assert fwt.start_flash() and fwt.step == "unloading"
    assert wait(app, lambda: fwt.step in ("closing", "hold_boot"), 10)
    assert not cam.uploaded
    fwt.abort()


def test_stale_sdkconfig_is_found_and_regenerated(tmp_path):
    (tmp_path / "sdkconfig.defaults").write_text(
        "# a comment\nCONFIG_PARTITION_TABLE_CUSTOM=y\nCONFIG_X=1\n")
    assert I.stale_sdkconfig(tmp_path) == []                 # no sdkconfig yet: fine
    (tmp_path / "sdkconfig").write_text("CONFIG_X=1\nCONFIG_PARTITION_TABLE_SINGLE_APP=y\n")
    assert I.stale_sdkconfig(tmp_path) == ["CONFIG_PARTITION_TABLE_CUSTOM=y"]
    old = I.regenerate_sdkconfig(tmp_path)
    assert old.name == "sdkconfig.old" and not (tmp_path / "sdkconfig").exists()
    assert I.stale_sdkconfig(tmp_path) == []

