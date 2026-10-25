"""圖形介面互動測試：開啟主視窗，依序按下每個按鈕（對話框自動回答），確認全程沒有錯誤。"""
import gc

import pytest

tk = pytest.importorskip("tkinter")


def test_every_gui_button_works(tmp_path, monkeypatch):
    monkeypatch.setenv("NAS115SYNC_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))  # 非 Windows 的資源回收筒位置
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("沒有圖形顯示環境")
    from nas115sync.app import build_app
    from nas115sync.gui import exercise
    from nas115sync.gui.app import MainWindow

    app = build_app(demo=True)
    s = app.config.settings
    s.stable_seconds, s.savedata_quiet_seconds, s.api_min_interval = 0.5, 1, 0
    win = MainWindow(root, app)
    try:
        summary = exercise.run(win, str(tmp_path / "work"))
        assert len(summary["steps"]) >= 15 and summary["opened"] > 0
        win._on_close()  # 沒有工作進行中：直接關閉視窗
        assert not _alive(root)
    finally:
        win.scheduler.stop()
        if _alive(root):
            root.destroy()
        app.db.close()
        del win
        gc.collect()  # 在主執行緒釋放視窗物件（Tk 不允許其他執行緒釋放）


def _alive(root) -> bool:
    try:
        return bool(root.winfo_exists())
    except tk.TclError:
        return False
