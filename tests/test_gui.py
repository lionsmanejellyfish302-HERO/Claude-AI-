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


def test_qr_login_dialog_flow():
    """115 掃碼登入：畫出白底 QR 碼 → 等待掃描 → 手機確認後完成登入並關閉對話框。"""
    import threading
    import time

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("沒有圖形顯示環境")
    from nas115sync.cloud.web115 import QR_CONFIRMED, QR_SCANNED, QR_WAITING, QrLogin
    from nas115sync.gui import theme
    from nas115sync.gui.dialogs import QrLoginDialog
    from nas115sync.gui.util import UiQueue

    class QrCloud:
        device = "alipaymini"
        finished = started = None

        def __init__(self):
            self.go = threading.Event()
            self.states = [QR_SCANNED, QR_CONFIRMED]

        def start_qr_login(self, device=None):
            self.started = device
            return QrLogin("1", 0, "s", "https://115.com/scan/dg-1", device)

        def poll_qr_status(self, login):
            return self.states.pop(0) if self.go.wait(0.05) else QR_WAITING

        def finish_qr_login(self, login):
            self.finished = login

    def pump(cond):
        end = time.monotonic() + 10
        while not cond() and time.monotonic() < end:
            root.update()
            time.sleep(0.01)
        return cond()

    try:
        theme.build(root, "dark")
        cloud = QrCloud()
        dlg = QrLoginDialog(root, cloud, UiQueue(root))
        dlg.start()
        assert pump(lambda: len(dlg.canvas.find_all()) > 100), "沒有畫出 QR 碼"
        assert dlg.canvas.cget("bg") == "white" and cloud.started == "alipaymini"  # 深色模式也是白底
        cloud.go.set()
        assert pump(lambda: not dlg.winfo_exists()), "確認後對話框沒有關閉"
        assert dlg.result is True and cloud.finished.uid == "1"
    finally:
        root.destroy()


def _alive(root) -> bool:
    try:
        return bool(root.winfo_exists())
    except tk.TclError:
        return False
