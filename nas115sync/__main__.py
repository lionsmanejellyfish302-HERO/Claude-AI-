"""指令列入口。不帶參數時開啟圖形介面。

  python -m nas115sync                      開啟圖形介面
  python -m nas115sync --demo gui           示範模式（模擬 115，不需帳號）
  python -m nas115sync login --client-id ID 以 115 App 掃 QR 碼登入
  python -m nas115sync mapping add "\\\\NAS\\home\\Photos" "/備份/Photos"
  python -m nas115sync mapping confirm <ID> 本人核對路徑
  python -m nas115sync scan                 比對（不上傳），產生待核對清單
  python -m nas115sync sync                 比對並執行自動同步
  python -m nas115sync reviews              列出待核對項目
  python -m nas115sync decide <ID> <決定>   處理待核對項目
  python -m nas115sync watch                即時同步（Ctrl+C 結束）
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from .app import build_app, setup_logging
from .compare import KIND_LABELS, available_decisions

log = logging.getLogger("nas115sync")


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def _progress(stage: str, done: int, total: int, text: str) -> None:
    if total:
        pct = done * 100 // max(1, total)
        print(f"\r{stage} {pct:3d}%  {text[-60:]:<60}", end="", flush=True)
    else:
        print(f"\r{stage} {done}  {text[-60:]:<60}", end="", flush=True)


def _ask(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() in ("y", "yes", "是")
    except EOFError:
        return False


def cmd_login(app, args) -> int:
    cloud = app.cloud
    if app.demo:
        print("示範模式不需要登入")
        return 0
    if args.client_id:
        app.config.settings.client_id = args.client_id
        app.config.save()
        cloud.client_id = args.client_id
    if args.refresh_token:
        cloud.login_with_refresh_token(args.refresh_token)
    else:
        from .cloud.open115 import QR_CONFIRMED

        login = cloud.start_qr_login()
        print("請用 115 App 掃描以下 QR 碼並確認授權：")
        try:
            import qrcode

            qr = qrcode.QRCode(border=1)
            qr.add_data(login.qrcode)
            qr.print_ascii(invert=True)
        except ImportError:
            print(login.qrcode)
        while True:
            status = cloud.poll_qr_status(login)
            if status == QR_CONFIRMED:
                break
            if status < 0:
                print("QR 碼已過期或取消，請重新執行 login")
                return 1
            if status == 1:
                print("已掃描，請在手機上確認…")
            time.sleep(1)
        cloud.finish_qr_login(login)
    print(f"登入成功：{cloud.user_label()}")
    return 0


def cmd_whoami(app, args) -> int:
    print(app.cloud.user_label() or "（未登入）")
    return 0


def cmd_mapping(app, args) -> int:
    from .mapping_check import inspect_mapping, suggest_mappings

    cfg = app.config
    if args.action == "list":
        for m in cfg.mappings:
            flag = "✔ 已核對" if m.confirmed else "✘ 待核對"
            extra = "（遊戲存檔）" if m.savedata else ""
            print(f"{m.id}  {flag}  {m.local_path}  ⇄  {m.cloud_path}{extra}")
        return 0
    if args.action == "add":
        m = cfg.add_mapping(args.local, args.cloud, savedata=args.savedata)
        print(f"已新增對應 {m.id}（尚未核對，請執行：mapping confirm {m.id}）")
        return 0
    if args.action == "suggest":
        for s in suggest_mappings(args.local, args.cloud, app.cloud):
            mark = "115 已有同名資料夾" if s.cloud_exists else "115 將新建"
            print(f"{s.local_path}  ⇄  {s.cloud_path}   [{mark}]")
            if args.add:
                cfg.add_mapping(s.local_path, s.cloud_path)
        if args.add:
            print("已加入為「待核對」對應，請逐一執行 mapping confirm 核對")
        return 0
    m = cfg.get_mapping(args.id or "")
    if m is None:
        print("找不到此對應 ID")
        return 1
    if args.action == "remove":
        cfg.remove_mapping(m.id)
        print("已刪除對應")
        return 0
    if args.action == "unconfirm":
        cfg.unconfirm_mapping(m.id)
        print("已取消核對，此對應不會再同步")
        return 0
    # confirm
    info = inspect_mapping(m, app.cloud, cfg.mappings)
    print("\n".join(info.lines(m)))
    if not info.local_ok:
        print("NAS 路徑無法存取，不能確認")
        return 1
    if not args.yes and not _ask("\n請核對以上兩邊路徑內容。確認此對應正確並開始同步？(yes/no) "):
        print("未確認")
        return 1
    cid = info.cloud_cid
    if cid is None:
        if not args.yes and not _ask(f"115 上沒有 {m.cloud_path}，要建立嗎？(yes/no) "):
            return 1
        cid = app.cloud.ensure_path(m.cloud_path)
    cfg.confirm_mapping(m.id, cid)
    print("已核對確認")
    return 0


def _print_plans(app, plans) -> None:
    for p in plans:
        m = app.config.get_mapping(p.mapping_id)
        s = p.summary()
        print(f"\n[{m.local_path} ⇄ {m.cloud_path}]")
        for w in p.warnings:
            print(f"  ⚠ {w}")
        print(f"  待上傳 {s['uploads']} 個（{_human(s['upload_bytes'])}）、已相同 {s['identical']}、"
              f"待核對 {s['reviews']}、僅 115 有 {s['cloud_only']}、自動刪除 {s['deletes']}、"
              f"新建資料夾 {s['mkdirs']}、暫時無法讀取 {s['unreadable']}")


def cmd_scan(app, args) -> int:
    plans = app.engine.analyze(progress=_progress)
    print()
    _print_plans(app, plans)
    print(f"\n待核對總數：{app.db.count_pending()}（執行 reviews 查看）")
    return 0


def cmd_sync(app, args) -> int:
    plans = app.engine.analyze(progress=_progress)
    print()
    _print_plans(app, plans)
    total = sum(len(p.uploads) + len(p.deletes) for p in plans)
    if total and not args.yes and not _ask(f"\n執行以上 {total} 項自動同步？(yes/no) "):
        return 1
    stats = app.engine.execute(plans, progress=_progress)
    print(f"\n完成：上傳 {stats['uploaded']}（秒傳 {stats['instant']}）、失敗 {stats['failed']}、"
          f"刪除 {stats['deleted']}、連結 {stats['linked']}")
    print(f"待核對總數：{app.db.count_pending()}")
    return 0


def cmd_reviews(app, args) -> int:
    for rv in app.db.list_reviews("pending", kind=args.kind):
        d = rv.details
        print(f"#{rv.id} [{KIND_LABELS.get(rv.kind, rv.kind)}] {rv.rel_path or '(整個對應)'}")
        print(f"    {d.get('message', '')}")
        loc = d.get("local") or {}
        if loc:
            print(f"    NAS：{loc.get('path')}  {_human(loc.get('size') or 0)}  MD5={loc.get('md5', '')}"
                  f"  SHA-1={loc.get('sha1', '')}")
        for c in d.get("cloud") or []:
            print(f"    115：{c.get('path')}  {_human(c.get('size') or 0)}  SHA-1={c.get('sha1', '')}")
        for mt in (d.get("matches") or [])[:5]:
            print(f"    相同{mt.get('hash')}：[{mt.get('where')}] {mt.get('path') or mt.get('rel_path')}")
        opts = ", ".join(f"{k}={v}" for k, v in available_decisions(rv.kind, d))
        print(f"    可用決定：{opts}")
    return 0


def cmd_decide(app, args) -> int:
    extra = {"process_names": args.process} if args.process else {}
    print(app.engine.apply_review(args.id, args.decision, extra))
    return 0


def cmd_watch(app, args) -> int:
    from .watcher import RealtimeSync

    rt = RealtimeSync(app.engine)
    rt.start()
    print("即時同步中，按 Ctrl+C 結束…")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        rt.stop()
    return 0


def cmd_gui(app, args) -> int:
    from .gui.app import run_gui

    run_gui(app, start_realtime=args.start_realtime, minimized=args.minimized)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nas115sync", description="NAS ⇄ 115 網盤比對與即時同步")
    ap.add_argument("--demo", action="store_true", help="示範模式：以模擬的 115 執行，不需帳號")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--start-realtime", action="store_true", help="（圖形介面）啟動後立即開始即時同步")
    ap.add_argument("--minimized", action="store_true", help="（圖形介面）啟動時縮到工作列")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("login", help="登入 115 開放平台")
    p.add_argument("--client-id", help="115 開放平台 AppID")
    p.add_argument("--refresh-token", help="直接使用已取得的 refresh_token")
    sub.add_parser("whoami", help="顯示目前登入的 115 帳號")

    p = sub.add_parser("mapping", help="管理 NAS ⇄ 115 路徑對應")
    msub = p.add_subparsers(dest="action", required=True)
    msub.add_parser("list")
    q = msub.add_parser("add")
    q.add_argument("local")
    q.add_argument("cloud")
    q.add_argument("--savedata", action="store_true", help="整個資料夾都是遊戲存檔")
    q = msub.add_parser("suggest", help="依名稱自動建議 NAS 根目錄下所有資料夾的對應")
    q.add_argument("local", help="NAS 根目錄，例如 \\\\NAS\\home 或 Z:\\")
    q.add_argument("cloud", help="115 根目錄，例如 /NAS備份")
    q.add_argument("--add", action="store_true", help="把建議加入為待核對對應")
    for name in ("confirm", "unconfirm", "remove"):
        q = msub.add_parser(name)
        q.add_argument("id")
        if name == "confirm":
            q.add_argument("--yes", action="store_true", help="不詢問直接確認（自動化用）")

    sub.add_parser("scan", help="比對但不上傳")
    p = sub.add_parser("sync", help="比對並執行自動同步")
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("reviews", help="列出待核對項目")
    p.add_argument("--kind")
    p = sub.add_parser("decide", help="處理待核對項目")
    p.add_argument("id", type=int)
    p.add_argument("decision")
    p.add_argument("--process", nargs="*", help="（存檔群組核可時）遊戲執行檔名稱，例如 eldenring.exe")
    sub.add_parser("watch", help="即時同步")
    sub.add_parser("gui", help="開啟圖形介面")

    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    app = build_app(demo=args.demo)
    handlers = {"login": cmd_login, "whoami": cmd_whoami, "mapping": cmd_mapping, "scan": cmd_scan,
                "sync": cmd_sync, "reviews": cmd_reviews, "decide": cmd_decide, "watch": cmd_watch,
                "gui": cmd_gui, None: cmd_gui}
    try:
        return handlers[args.cmd](app, args)
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001
        log.debug("錯誤", exc_info=True)
        print(f"錯誤：{e}", file=sys.stderr)
        return 1
    finally:
        app.db.close()


if __name__ == "__main__":
    sys.exit(main())
