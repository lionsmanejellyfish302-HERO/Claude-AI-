"""指令列入口。不帶參數時開啟圖形介面。

  python -m nas115sync                      開啟圖形介面
  python -m nas115sync --demo gui           示範模式（模擬 115，不需帳號）
  python -m nas115sync login                用 115 App 掃 QR 碼登入（與 115 電腦版同一個帳號）
  python -m nas115sync check                115 連線測試（上傳、秒傳、下載、改名、刪除）
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

from . import __version__
from .app import build_app, setup_logging
from .compare import KIND_LABELS, available_decisions
from .config import DIRECTION_LABELS

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
    from .cloud.web115 import LOGIN_DEVICES, QR_CONFIRMED

    cloud = app.engine.client(args.account)
    if app.demo:
        print("示範模式不需要登入")
        return 0
    if args.device not in LOGIN_DEVICES:
        print("可用的登入裝置欄位：")
        for k, v in LOGIN_DEVICES.items():
            print(f"  {k:12s} {v}")
        return 2
    login = cloud.start_qr_login(args.device)
    print(f"登入裝置欄位：{LOGIN_DEVICES[args.device]}")
    print("請用 115 App 掃描以下 QR 碼並在手機上確認：")
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


def cmd_check(app, args) -> int:
    from .check115 import run_check

    cloud = app.engine.client(args.account)
    print("115 連線測試（只在 115 建立一個測試資料夾，測完刪除）：")
    results = run_check(cloud, lambda name, ok, text: print(f"  {'✔' if ok else '✘'} {name}：{text}"))
    ok = all(r[1] for r in results)
    print("全部通過" if ok else "有項目失敗，請查看上面的說明")
    return 0 if ok else 1


def cmd_whoami(app, args) -> int:
    for a in app.config.accounts:
        try:
            label = app.engine.client(a.id).user_label() or "（未登入）"
        except Exception as e:  # noqa: BLE001
            label = f"（未登入：{e}）"
        print(f"{a.id}  {a.label}：{label}")
    return 0


def cmd_accounts(app, args) -> int:
    from .app import make_client

    cfg = app.config
    if args.action == "add":
        a = cfg.add_account(args.label)
        app.engine.clouds[a.id] = make_client(cfg, a.id, app.demo)
        print(f"已新增帳號 {a.id}（{a.label}），請執行：login --account {a.id}")
    elif args.action == "remove":
        cfg.remove_account(args.id)
        print("已刪除帳號")
    else:
        for a in cfg.accounts:
            used = sum(m.account_id == a.id for m in cfg.mappings)
            print(f"{a.id}  {a.label}（{used} 個對應）")
    return 0


def _resolve_cloud(app, account: str, path: str) -> str:
    cid = app.engine.client(account).resolve_path(path)
    if cid is None:
        raise SystemExit(f"115 上找不到：{path}")
    return cid


def cmd_ls(app, args) -> int:
    c = app.engine.client(args.account)
    for e in c.list_dir(_resolve_cloud(app, args.account, args.path)):
        kind = "📁" if e.is_dir else "  "
        print(f"{kind} {e.name:<50} {'' if e.is_dir else _human(e.size):>10}  {e.sha1}")
    return 0


def cmd_search(app, args) -> int:
    c = app.engine.client(args.account)
    for e in c.search(args.keyword, _resolve_cloud(app, args.account, args.within)):
        print(f"{'📁' if e.is_dir else '  '} {e.name}  （資料夾 ID {e.pid}）  {'' if e.is_dir else _human(e.size)}")
    return 0


def cmd_upload(app, args) -> int:
    from . import manual

    cid = app.engine.client(args.account).ensure_path(args.cloud)
    stats = manual.upload_paths(app.engine, args.account, args.local, cid, args.conflict)
    print(f"秒傳 {stats['instant']}、實際上傳 {stats['uploaded']}、略過 {stats['skipped']}、失敗 {stats['failed']}")
    return 0 if not stats["failed"] else 1


def cmd_download(app, args) -> int:
    from . import manual
    from .paths import cloud_parts

    c = app.engine.client(args.account)
    parts = cloud_parts(args.cloud)
    parent = _resolve_cloud(app, args.account, "/" + "/".join(parts[:-1]))
    entry = c.find_child(parent, parts[-1]) if parts else None
    if entry is None:
        raise SystemExit(f"115 上找不到：{args.cloud}")
    stats = manual.download_entries(app.engine, args.account, [entry], args.local, args.conflict)
    print(f"下載 {stats['downloaded']}（其中從 NAS 複製 {stats['copied']}）、略過 {stats['skipped']}、"
          f"失敗 {stats['failed']}")
    return 0 if not stats["failed"] else 1


def cmd_dupes(app, args) -> int:
    from . import manual

    if args.scan:
        app.engine.analyze(progress=_progress)
        print()
    groups = manual.duplicate_report(app.engine)
    for g in groups[: args.limit]:
        print(f"\nSHA-1 {g['sha1']}  {_human(g['size'])}  NAS {g['nas_count']} 份、115 {g['cloud_count']} 份、"
              f"多佔 {_human(g['wasted'])}")
        for i in g["items"]:
            print(f"   [{i['where']}] {i['path']}")
    print(f"\n共 {len(groups)} 組重複檔案，可省下 {_human(sum(g['wasted'] for g in groups))}")
    return 0


def cmd_schedule(app, args) -> int:
    from .scheduler import describe, new_schedule

    s = app.config.settings
    if args.action == "add":
        days = sorted({int(d) for part in args.days for d in part.split(",") if d.strip()}) if args.days else None
        if days is not None and any(d < 0 or d > 6 for d in days):
            raise SystemExit("星期請用 0（一）到 6（日）")
        s.schedules.append(new_schedule(args.time, days, args.action_type))
        app.config.save()
    elif args.action == "remove":
        s.schedules = [x for x in s.schedules if x["id"] != args.id]
        app.config.save()
    for x in s.schedules:
        print(f"{x['id']}  {describe(x)}")
    return 0


def cmd_mapping(app, args) -> int:
    from .mapping_check import inspect_mapping, suggest_mappings

    cfg = app.config
    if args.action == "list":
        for m in cfg.mappings:
            flag = "✔ 已核對" if m.confirmed else "✘ 待核對"
            extra = "（遊戲存檔）" if m.savedata else ""
            print(f"{m.id}  {flag}  {m.local_path}  ⇄  {m.cloud_path}{extra}  [{DIRECTION_LABELS[m.direction]}]")
        return 0
    if args.action == "add":
        m = cfg.add_mapping(args.local, args.cloud, savedata=args.savedata, direction=args.direction,
                            delete_mode=args.delete_mode, account_id=args.account,
                            include_patterns=args.include or [], exclude_patterns=args.exclude or [],
                            min_size=args.min_size, max_size=args.max_size, skip_hidden=args.skip_hidden)
        print(f"已新增對應 {m.id}（尚未核對，請執行：mapping confirm {m.id}）")
        return 0
    if args.action == "suggest":
        for s in suggest_mappings(args.local, args.cloud, app.engine.client(args.account)):
            mark = "115 已有同名資料夾" if s.cloud_exists else "115 將新建"
            print(f"{s.local_path}  ⇄  {s.cloud_path}   [{mark}]")
            if args.add:
                cfg.add_mapping(s.local_path, s.cloud_path, account_id=args.account)
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
    info = inspect_mapping(m, app.engine.cloud_for(m), cfg.mappings)
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
        cid = app.engine.cloud_for(m).ensure_path(m.cloud_path)
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
        print(f"  [{DIRECTION_LABELS[m.direction]}] 待上傳 {s['uploads']} 個（{_human(s['upload_bytes'])}）、"
              f"待下載 {s['downloads']} 個（{_human(s['download_bytes'])}）、已相同 {s['identical']}、"
              f"待核對 {s['reviews']}、僅 115 有 {s['cloud_only']}、僅 NAS 有 {s['local_only']}、"
              f"刪除 115 {s['deletes']}／NAS {s['local_deletes']}、新建資料夾 {s['mkdirs']}、"
              f"暫時無法讀取 {s['unreadable']}")


def cmd_scan(app, args) -> int:
    plans = app.engine.analyze(progress=_progress)
    print()
    _print_plans(app, plans)
    if args.csv:
        from .manual import export_plans_csv

        print(f"已匯出 {export_plans_csv(app.engine, plans, args.csv)} 筆到 {args.csv}")
    print(f"\n待核對總數：{app.db.count_pending()}（執行 reviews 查看）")
    return 0


def cmd_sync(app, args) -> int:
    plans = app.engine.analyze(progress=_progress)
    print()
    _print_plans(app, plans)
    total = sum(len(p.uploads) + len(p.downloads) + len(p.deletes) + len(p.local_deletes) for p in plans)
    if total and not args.yes and not _ask(f"\n執行以上 {total} 項自動同步？(yes/no) "):
        return 1
    stats = app.engine.execute(plans, progress=_progress)
    print(f"\n完成：上傳 {stats['uploaded']}（秒傳 {stats['instant']}）、下載 {stats['downloaded']}"
          f"（從 NAS 複製 {stats['copied']}）、失敗 {stats['failed']}、刪除 115 {stats['deleted']}／"
          f"NAS {stats['local_deleted']}、連結 {stats['linked']}")
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

    from .scheduler import Scheduler

    rt = RealtimeSync(app.engine)
    rt.start()
    Scheduler(app.engine).start()
    print("即時同步中（排程也會執行），按 Ctrl+C 結束…")
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


def _utf8_console() -> None:
    """輸出導向檔案或管線時（排程、記錄）Windows 預設編碼無法輸出中文，統一改用 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure") \
                and (getattr(stream, "encoding", "") or "").lower().replace("-", "") != "utf8":
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    ap = argparse.ArgumentParser(prog="nas115sync", description="NAS ⇄ 115 網盤比對與即時同步")
    ap.add_argument("--demo", action="store_true", help="示範模式：以模擬的 115 執行，不需帳號")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--start-realtime", action="store_true", help="（圖形介面）啟動後立即開始即時同步")
    ap.add_argument("--minimized", action="store_true", help="（圖形介面）啟動時縮到工作列")
    ap.add_argument("--version", action="store_true", help="顯示版本")
    ap.add_argument("--self-test", metavar="報告資料夾", help="自我測試（模擬 115，不影響正式設定）")
    ap.add_argument("--no-gui", action="store_true", help="（自我測試）略過圖形介面")
    ap.add_argument("--tab-seconds", type=float, default=0, help="（自我測試）每個分頁停留秒數")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("login", help="用 115 App 掃 QR 碼登入（與 115 電腦版相同的登入方式）")
    p.add_argument("--device", default="alipaymini",
                   help="登入裝置欄位（115 每種裝置只允許一個登入；預設 alipaymini，不影響 115 電腦版與手機 App）")
    p.add_argument("--account", default="default", help="帳號 ID（多帳號時）")
    p = sub.add_parser("check", help="115 連線測試：上傳、秒傳、下載、改名、刪除（只動測試資料夾）")
    p.add_argument("--account", default="default", help="帳號 ID（多帳號時）")
    sub.add_parser("whoami", help="顯示所有 115 帳號的登入狀態")
    p = sub.add_parser("accounts", help="管理多個 115 帳號")
    asub = p.add_subparsers(dest="action")
    asub.add_parser("list")
    q = asub.add_parser("add")
    q.add_argument("label")
    q = asub.add_parser("remove")
    q.add_argument("id")

    p = sub.add_parser("mapping", help="管理 NAS ⇄ 115 路徑對應")
    msub = p.add_subparsers(dest="action", required=True)
    msub.add_parser("list")
    q = msub.add_parser("add")
    q.add_argument("local")
    q.add_argument("cloud")
    q.add_argument("--savedata", action="store_true", help="整個資料夾都是遊戲存檔")
    q.add_argument("--direction", choices=["upload", "download", "two_way"], default="upload",
                   help="upload=NAS→115（預設） download=115→NAS two_way=雙向")
    q.add_argument("--delete-mode", choices=["", "review", "auto", "ignore"], default="")
    q.add_argument("--account", default="default")
    q.add_argument("--include", nargs="*", help="只同步符合的檔名，例如 *.jpg *.png")
    q.add_argument("--exclude", nargs="*", help="不同步符合的檔名/資料夾，例如 *.iso cache")
    q.add_argument("--min-size", type=int, default=0, help="最小檔案大小（bytes）")
    q.add_argument("--max-size", type=int, default=0, help="最大檔案大小（bytes）")
    q.add_argument("--skip-hidden", action="store_true", help="略過隱藏/系統檔")
    q = msub.add_parser("suggest", help="依名稱自動建議 NAS 根目錄下所有資料夾的對應")
    q.add_argument("local", help="NAS 根目錄，例如 \\\\NAS\\home 或 Z:\\")
    q.add_argument("cloud", help="115 根目錄，例如 /NAS備份")
    q.add_argument("--add", action="store_true", help="把建議加入為待核對對應")
    q.add_argument("--account", default="default")
    for name in ("confirm", "unconfirm", "remove"):
        q = msub.add_parser(name)
        q.add_argument("id")
        if name == "confirm":
            q.add_argument("--yes", action="store_true", help="不詢問直接確認（自動化用）")

    p = sub.add_parser("scan", help="比對但不上傳")
    p.add_argument("--csv", help="把比對結果匯出成 CSV 檔")
    p = sub.add_parser("sync", help="比對並執行自動同步")
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("reviews", help="列出待核對項目")
    p.add_argument("--kind")
    p = sub.add_parser("decide", help="處理待核對項目")
    p.add_argument("id", type=int)
    p.add_argument("decision")
    p.add_argument("--process", nargs="*", help="（存檔群組核可時）遊戲執行檔名稱，例如 eldenring.exe")
    sub.add_parser("watch", help="即時同步（含排程）")
    p = sub.add_parser("ls", help="列出 115 資料夾內容")
    p.add_argument("path", nargs="?", default="/")
    p.add_argument("--account", default="default")
    p = sub.add_parser("search", help="搜尋 115 檔名")
    p.add_argument("keyword")
    p.add_argument("--within", default="/", help="只搜尋此 115 資料夾底下")
    p.add_argument("--account", default="default")
    for name, helptext in (("upload", "手動上傳 NAS 檔案/資料夾到 115（秒傳優先）"),
                           ("download", "手動下載 115 檔案/資料夾到 NAS（可續傳、驗證 SHA-1）")):
        p = sub.add_parser(name, help=helptext)
        if name == "upload":
            p.add_argument("local", nargs="+")
            p.add_argument("cloud", help="115 目的資料夾（不存在會建立）")
        else:
            p.add_argument("cloud", help="115 檔案或資料夾路徑")
            p.add_argument("local", help="NAS 目的資料夾")
        p.add_argument("--conflict", choices=["keep_both", "overwrite", "skip"], default="keep_both",
                       help="同名但內容不同時：保留兩者（預設）/覆蓋/略過")
        p.add_argument("--account", default="default")
    p = sub.add_parser("dupes", help="列出 NAS 與 115 上的重複檔案（依 SHA-1）")
    p.add_argument("--scan", action="store_true", help="先做完整比對以更新索引")
    p.add_argument("--limit", type=int, default=50)
    p = sub.add_parser("schedule", help="管理排程")
    ssub = p.add_subparsers(dest="action")
    ssub.add_parser("list")
    q = ssub.add_parser("add")
    q.add_argument("time", help="HH:MM，例如 03:00")
    q.add_argument("--days", nargs="+", help="星期（0=一 … 6=日），例如 0,2,4 或 0 2 4；預設每天")
    q.add_argument("--action-type", choices=["sync", "scan"], default="sync")
    q = ssub.add_parser("remove")
    q.add_argument("id")
    sub.add_parser("gui", help="開啟圖形介面")

    args = ap.parse_args(argv)
    if args.version:
        print(f"NAS115Sync {__version__}")
        return 0
    if args.self_test:
        from .selftest import run

        code = run(args.self_test, gui=not args.no_gui, tab_seconds=args.tab_seconds)
        print(f"自我測試{'通過' if code == 0 else '失敗'}，報告：{args.self_test}")
        return code
    setup_logging(args.verbose)
    app = build_app(demo=args.demo)
    handlers = {"login": cmd_login, "check": cmd_check, "whoami": cmd_whoami, "accounts": cmd_accounts, "mapping": cmd_mapping,
                "scan": cmd_scan, "sync": cmd_sync, "reviews": cmd_reviews, "decide": cmd_decide,
                "watch": cmd_watch, "ls": cmd_ls, "search": cmd_search, "upload": cmd_upload,
                "download": cmd_download, "dupes": cmd_dupes, "schedule": cmd_schedule,
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
