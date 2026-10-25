"""115 網盤客戶端：使用與 115 電腦版／網頁版相同的登入與檔案介面（不需要 open.115.com 開發者 AppID）。

- 登入：115 App 掃 QR 碼，取得與 115 電腦版相同格式的登入 cookies（以 DPAPI 加密保存）
  115 每種「登入裝置」只允許一個連線，本程式預設使用「支付寶小程序」裝置欄位，
  不會把你電腦上的 115 電腦版（Windows 裝置）或手機 App 擠下線。
- 檔案：與 115 電腦版看到的是同一個網盤；這裡上傳、改名、刪除，115 電腦版會立刻看到，反之亦然。
- 上傳：initupload 帶整檔 SHA-1 → 115 端已有相同內容即「秒傳」；要求二次驗證時計算指定區段 SHA-1 重送；
  仍無法秒傳才取得 OSS 憑證實際上傳（大檔分片、可斷點續傳）。
- 115 端變動：讀取 115 的「操作紀錄」偵測在 115 電腦版／App 上的新增、修改、改名、搬移、刪除。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass

import requests

from .. import __version__
from ..hashing import FileHashes, sha1_range
from ..paths import cloud_norm, cloud_parts, long_path, nfc
from ..transfers import TransferControl
from . import web115_codec as codec
from .base import AuthError, CloudChange, CloudClient, CloudEntry, CloudError, NotFoundError, SessionStore, UploadResult
from .tokens import TokenStore

log = logging.getLogger(__name__)

WEBAPI = "https://webapi.115.com"
PROAPI = "https://proapi.115.com"
QRAPI = "https://qrcodeapi.115.com"
UPLB = "https://uplb.115.com"
OSS_ENDPOINT = "https://oss-cn-shenzhen.aliyuncs.com"
USER_AGENT = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) NAS115Sync/{__version__}"
UPLOAD_UA = (f"Mozilla/5.0 115disk/{codec.APP_VERSION} 115Browser/{codec.APP_VERSION} "
             f"115wangpan_android/{codec.APP_VERSION}")
PAGE_LIMIT = 1150
COOKIE_NAMES = ("UID", "CID", "SEID", "KID")

QR_WAITING, QR_SCANNED, QR_CONFIRMED = 0, 1, 2
STATUS_NEED_UPLOAD, STATUS_INSTANT, STATUS_SIGN_CHECK = 1, 2, 7

# 可選的登入裝置欄位（115 每種裝置只允許一個登入，選你「沒有在用」的那一種）
LOGIN_DEVICES = {
    "alipaymini": "支付寶小程序（建議，不影響 115 電腦版與手機 App）",
    "wechatmini": "微信小程序",
    "harmony": "鴻蒙版",
    "tv": "安卓電視版",
    "os_mac": "macOS 版（沒有 Mac 時可用）",
    "os_linux": "Linux 版",
    "115ipad": "iPad 版",
}
DEFAULT_DEVICE = "alipaymini"
# 操作紀錄中只是瀏覽、加星標等不影響檔案內容的事件
_IGNORED_EVENTS = {3, 4, 7, 8, 9, 10, 19}


@dataclass
class QrLogin:
    uid: str
    time: int
    sign: str
    qrcode: str  # 要編成 QR 碼給 115 App 掃描的內容
    device: str = DEFAULT_DEVICE


# --------------------------------------------------------------------------- 回應解析
def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _ok(j) -> bool:
    if not isinstance(j, dict):
        return False
    state = j.get("state")
    if state is None:
        return _int(j.get("errno", j.get("errNo", j.get("code", 0)))) == 0
    return state in (True, 1, "1", "true")


def _err(j: dict) -> tuple[object, str]:
    code = j.get("errno", j.get("errNo", j.get("code")))
    msg = j.get("error") or j.get("message") or j.get("msg") or j.get("error_msg") or ""
    return code, str(msg)


def _is_auth(code, msg: str) -> bool:
    return str(code) in {"99", "990001", "990002", "40100000"} or any(
        k in msg for k in ("重新登录", "重新登錄", "登录超时", "登錄逾時", "未登录", "未登入", "请登录", "請登入"))


def _is_busy(code, msg: str) -> bool:
    return str(code) in {"406", "770004"} or any(
        k in msg for k in ("频繁", "頻繁", "太快", "稍后", "稍後", "过多", "過多"))


def _needs_verify(code, msg: str) -> bool:
    return str(code) == "911" or any(k in msg for k in ("验证", "驗證", "安全"))


def _first(d):
    if isinstance(d, list):
        return d[0] if d else {}
    return d if isinstance(d, dict) else {}


def parse_entry(it: dict) -> CloudEntry:
    """webapi 的檔案資料：資料夾有 cid（自己）與 pid（上層）；檔案有 fid（自己）與 cid（上層）。"""
    if "file_category" in it:
        is_dir = str(it["file_category"]) == "0"
    elif "fc" in it:
        is_dir = str(it["fc"]) == "0"
    elif "sha" in it or "sha1" in it:
        is_dir = not (it.get("sha") or it.get("sha1"))
    else:
        is_dir = "fid" not in it and "file_id" not in it
    if is_dir:
        fid = it.get("cid") if "cid" in it and "fid" not in it else it.get("file_id") or it.get("fid")
        pid = it.get("pid", it.get("parent_id", ""))
    else:
        fid = it.get("fid") or it.get("file_id")
        pid = it.get("cid", it.get("parent_id", it.get("pid", "")))
    return CloudEntry(
        fid=str(fid or ""),
        name=nfc(str(it.get("n") or it.get("fn") or it.get("file_name") or "")),
        is_dir=is_dir,
        pid=str(pid if pid is not None else ""),
        size=_int(it.get("s", it.get("fs", it.get("file_size", 0)))),
        sha1=str(it.get("sha") or it.get("sha1") or "").upper(),
        pick_code=str(it.get("pc") or it.get("pick_code") or it.get("pickcode") or ""),
        mtime=_int(it.get("te") or it.get("tu") or it.get("t") or it.get("update_time") or 0),
    )


# --------------------------------------------------------------------------- 客戶端
class Web115Client(CloudClient):
    def __init__(self, token_store: TokenStore, device: str = DEFAULT_DEVICE, min_interval: float = 0.4,
                 part_size: int = 20 * 1024 * 1024, timeout: float = 60, session: requests.Session | None = None):
        super().__init__()
        self.tokens = token_store
        self.device = device if device in LOGIN_DEVICES else DEFAULT_DEVICE
        self.min_interval = min_interval
        self.part_size = part_size
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self._rate_lock = threading.Lock()
        self._last_call = 0.0
        self._userkey = ""
        self._data = token_store.load()
        self._apply_cookies()

    # ------------------------------------------------------------ 登入
    def _apply_cookies(self) -> None:
        self.session.cookies.clear()
        for k, v in (self._data.get("cookies") or {}).items():
            if k in COOKIE_NAMES and v:
                self.session.cookies.set(k, str(v), domain=".115.com", path="/")

    def is_logged_in(self) -> bool:
        c = self._data.get("cookies") or {}
        return all(c.get(k) for k in ("UID", "CID", "SEID"))

    @property
    def user_id(self) -> int:
        uid = str((self._data.get("cookies") or {}).get("UID") or "")
        user_id = _int(uid.split("_", 1)[0], 0)
        if not user_id:
            raise AuthError("尚未登入 115")
        return user_id

    def start_qr_login(self, device: str | None = None) -> QrLogin:
        device = device if device in LOGIN_DEVICES else self.device
        d = _first(self._qr("GET", f"{QRAPI}/api/1.0/web/1.0/token/").get("data"))
        if not d.get("uid"):
            raise AuthError(f"115 未回傳 QR 碼：{json.dumps(d, ensure_ascii=False)[:200]}")
        return QrLogin(str(d["uid"]), _int(d.get("time")), str(d.get("sign") or ""), str(d.get("qrcode") or ""),
                       device)

    def poll_qr_status(self, login: QrLogin, timeout: float = 35) -> int:
        """回傳 0=等待掃描 1=已掃描待確認 2=已確認；負數=過期或取消。"""
        try:
            r = self.session.get(f"{QRAPI}/get/status/", params={"uid": login.uid, "time": login.time,
                                                                 "sign": login.sign}, timeout=timeout)
            j = r.json()
        except requests.Timeout:
            return QR_WAITING
        except (requests.RequestException, ValueError) as e:
            raise AuthError(f"查詢 QR 碼狀態失敗：{e}") from e
        d = j.get("data") if isinstance(j.get("data"), dict) else j
        return _int(d.get("status"), QR_WAITING)

    def finish_qr_login(self, login: QrLogin) -> None:
        d = _first(self._qr("POST", f"{QRAPI}/app/1.0/{login.device}/1.0/login/qrcode/",
                            data={"account": login.uid}).get("data"))
        cookies = d.get("cookie") or {}
        if not all(cookies.get(k) for k in ("UID", "CID", "SEID")):
            raise AuthError("115 未回傳登入資訊，請重新掃碼")
        self._data = {"cookies": {k: str(cookies[k]) for k in COOKIE_NAMES if cookies.get(k)},
                      "device": login.device, "user_name": str(d.get("user_name") or ""),
                      "login_at": int(time.time())}
        self.device = login.device
        self._userkey = ""
        self.tokens.save(self._data)
        self._apply_cookies()

    def logout(self) -> None:
        self._data = {}
        self._userkey = ""
        self.tokens.clear()
        self.session.cookies.clear()

    def _qr(self, method: str, url: str, data: dict | None = None) -> dict:
        try:
            r = self.session.request(method, url, data=data, timeout=self.timeout)
            j = r.json()
        except (requests.RequestException, ValueError) as e:
            raise AuthError(f"連線 115 登入服務失敗：{e}") from e
        if not _ok(j):
            code, msg = _err(j)
            raise AuthError(msg or "115 登入失敗", code)
        return j

    # ------------------------------------------------------------ API 共用
    def _throttle(self) -> None:
        with self._rate_lock:
            wait = self._last_call + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    def _call(self, method: str, url: str, params: dict | None = None, data=None, headers: dict | None = None,
              check: bool = True, decrypt: bool = False) -> dict:
        if not self.is_logged_in():
            raise AuthError("尚未登入 115")
        for attempt in range(4):
            self._throttle()
            try:
                r = self.session.request(method, url, params=params, data=data, headers=headers,
                                         timeout=self.timeout)
            except requests.RequestException as e:
                if attempt == 3:
                    raise CloudError(f"連線 115 失敗：{e}") from e
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == 3:
                    raise CloudError(f"115 伺服器忙碌 HTTP {r.status_code}", r.status_code)
                time.sleep(2 ** (attempt + 1))
                continue
            if r.status_code in (401, 403):
                raise AuthError(f"115 拒絕存取（HTTP {r.status_code}），請重新登入", r.status_code)
            try:
                body = codec.decrypt_response(r.content) if decrypt else codec.maybe_decrypt(r.content)
                j = json.loads(body)
            except ValueError as e:
                raise CloudError(f"115 回應無法解析：HTTP {r.status_code}") from e
            if not check or _ok(j):
                return j
            code, msg = _err(j)
            if _is_auth(code, msg):
                raise AuthError(f"115 登入已失效，請重新掃碼登入（{msg or code}）", code)
            if _is_busy(code, msg) and attempt < 3:
                log.warning("115 要求放慢速度（%s），稍候重試", msg or code)
                time.sleep(5 * (attempt + 1))
                continue
            if _needs_verify(code, msg):
                raise CloudError(f"115 要求安全驗證，請先用 115 電腦版或網頁開啟網盤完成驗證：{msg}", code)
            raise CloudError(msg or json.dumps(j, ensure_ascii=False)[:300], code)
        raise CloudError("115 多次重試失敗")

    # ------------------------------------------------------------ 帳號
    def user_label(self) -> str:
        j = self._call("GET", "https://my.115.com/", params={"ct": "ajax", "ac": "nav"})
        d = _first(j.get("data"))
        name = str(d.get("user_name") or self._data.get("user_name") or "")
        return f"{name}（{self.user_id}）" if name else str(self.user_id)

    def _user_key(self) -> str:
        if not self._userkey:
            j = self._call("GET", f"{WEBAPI}/files/uploadinfo")
            self._userkey = str(j.get("userkey") or _first(j.get("data")).get("userkey") or "")
            if not self._userkey:
                raise CloudError("115 未回傳上傳金鑰（userkey）")
        return self._userkey

    # ------------------------------------------------------------ 檔案操作
    def list_dir(self, cid: str) -> list[CloudEntry]:
        out: list[CloudEntry] = []
        seen: set[str] = set()
        offset = 0
        while True:
            j = self._call("GET", f"{WEBAPI}/files", params={
                "aid": 1, "cid": cid, "offset": offset, "limit": PAGE_LIMIT, "show_dir": 1, "cur": 1,
                "o": "file_name", "asc": 1, "record_open_time": 0, "count_folders": 0, "format": "json"})
            # 115 對不存在的資料夾會靜默回傳根目錄的內容，必須檢查，否則會把別的資料夾當成這裡
            if "cid" in j and str(j.get("cid")) != str(cid):
                raise NotFoundError(f"115 資料夾不存在（cid={cid}）")
            items = [parse_entry(it) for it in (j.get("data") or []) if isinstance(it, dict)]
            new = [e for e in items if e.fid and e.fid not in seen]
            if not new:
                break
            seen.update(e.fid for e in new)
            out.extend(new)
            offset += len(items)
            if offset >= _int(j.get("count")):
                break
        return out

    def get_entry(self, fid: str) -> CloudEntry | None:
        try:
            j = self._call("GET", f"{WEBAPI}/files/get_info", params={"file_id": fid})
        except NotFoundError:
            return None
        except CloudError as e:
            if str(e.code) in {"20018", "50015", "70005"}:
                return None
            raise
        d = _first(j.get("data"))
        e = parse_entry(d) if d else None
        return e if e and e.fid else None

    def resolve_path(self, path: str) -> str | None:
        parts = cloud_parts(path)
        if not parts:
            return self.root_cid
        try:
            j = self._call("GET", f"{WEBAPI}/files/getid", params={"path": cloud_norm(path)})
            cid = str(j.get("id") or "0")
            return None if cid == "0" else cid
        except CloudError as e:
            log.debug("以路徑查詢 115 資料夾失敗，改為逐層查找：%s", e)
        return super().resolve_path(path)

    def mkdir(self, pid: str, name: str) -> str:
        try:
            j = self._call("POST", f"{WEBAPI}/files/add", data={"pid": pid, "cname": name})
        except CloudError as e:
            if str(e.code) == "20004" or "已存在" in str(e):
                existing = self.find_child(pid, name, want_dir=True)
                if existing:
                    return existing.fid
            raise
        cid = j.get("cid") or j.get("file_id") or _first(j.get("data")).get("cid")
        if not cid:
            raise CloudError(f"建立 115 資料夾失敗：{name}")
        return str(cid)

    def delete(self, fids: list[str], parent_id: str = "") -> None:
        if not fids:
            return
        data = {f"fid[{i}]": f for i, f in enumerate(fids)}
        data["ignore_warn"] = 1
        if parent_id:
            data["pid"] = parent_id
        self._call("POST", f"{WEBAPI}/rb/delete", data=data)

    def rename(self, fid: str, new_name: str) -> None:
        self._call("POST", f"{WEBAPI}/files/batch_rename", data={f"files_new_name[{fid}]": new_name})

    def move(self, fids: list[str], to_cid: str) -> None:
        if fids:
            data = {f"fid[{i}]": f for i, f in enumerate(fids)}
            data["pid"] = to_cid
            self._call("POST", f"{WEBAPI}/files/move", data=data)

    def search(self, keyword: str, cid: str = "0", limit: int = 500) -> list[CloudEntry]:
        out: list[CloudEntry] = []
        offset = 0
        while len(out) < limit:
            j = self._call("GET", f"{WEBAPI}/files/search", params={
                "search_value": keyword, "cid": cid, "aid": 1, "offset": offset,
                "limit": min(PAGE_LIMIT, limit - len(out)), "format": "json"})
            items = [parse_entry(it) for it in (j.get("data") or []) if isinstance(it, dict)]
            if not items:
                break
            out += items
            offset += len(items)
            if offset >= _int(j.get("count")):
                break
        return out

    # ------------------------------------------------------------ 115 端變動（115 電腦版／App 上的操作）
    def changes(self, cursor: str | None) -> tuple[list[CloudChange], str]:
        """回傳 cursor 之後的操作紀錄。第一次呼叫（cursor=None）只記下目前位置。"""
        j = self._call("GET", f"{WEBAPI}/behavior/detail", params={"limit": 100, "offset": 0})
        events = [e for e in (_first(j.get("data")).get("list") or []) if isinstance(e, dict)]
        newest = max((_int(e.get("id")) for e in events), default=_int(cursor))
        if cursor is None:
            return [], str(newest)
        last = _int(cursor)
        out = []
        for e in events:
            if _int(e.get("id")) <= last or _int(e.get("type")) in _IGNORED_EVENTS:
                continue
            out.append(CloudChange(fid=str(e.get("file_id") or ""),
                                   parent_id=str(e.get("parent_id") or e.get("pid") or e.get("cid") or ""),
                                   kind=str(e.get("type") or ""), name=nfc(str(e.get("file_name") or ""))))
        return out, str(max(newest, last))

    def recent_files(self, cid: str, limit: int = 50) -> list[CloudEntry]:
        """cid 底下（含子資料夾）最近修改的檔案，新到舊。"""
        j = self._call("GET", f"{WEBAPI}/files", params={
            "aid": 1, "cid": cid, "offset": 0, "limit": limit, "show_dir": 0, "cur": 0, "o": "user_utime",
            "asc": 0, "record_open_time": 0, "format": "json"})
        return [parse_entry(it) for it in (j.get("data") or []) if isinstance(it, dict)]

    # ------------------------------------------------------------ 上傳／秒傳
    def _init_upload(self, cid: str, name: str, h: FileHashes, sign_key: str = "", sign_val: str = "") -> dict:
        fields = {"appid": 0, "appversion": codec.APP_VERSION, "filename": name, "fileid": h.sha1.upper(),
                  "filesize": h.size, "target": f"U_1_{cid}", "topupload": "true", "userid": self.user_id,
                  "sign_key": sign_key, "sign_val": sign_val.upper(), "path": ""}
        params, body = codec.sign_upload(fields, self._user_key(), int(time.time()))
        return self._call("POST", f"{UPLB}/4.0/initupload.php", params=params, data=body, check=False,
                          decrypt=True, headers={"Content-Type": "application/x-www-form-urlencoded",
                                                 "User-Agent": UPLOAD_UA})

    def _init_with_sign_check(self, local_path: str, cid: str, name: str, h: FileHashes) -> dict:
        d = self._init_upload(cid, name, h)
        for _ in range(3):
            if _int(d.get("status")) != STATUS_SIGN_CHECK or not d.get("sign_check"):
                break
            start, _, end = str(d["sign_check"]).partition("-")
            val = sha1_range(local_path, int(start), int(end))
            log.debug("115 二次驗證 %s 區段 %s", name, d["sign_check"])
            d = self._init_upload(cid, name, h, sign_key=str(d.get("sign_key") or ""), sign_val=val)
        return d

    def _oss_token(self) -> dict:
        j = self._call("GET", f"{UPLB}/3.0/gettoken.php", check=False)
        if str(j.get("StatusCode")) != "200" or not j.get("AccessKeyId"):
            raise CloudError("無法取得 115 上傳憑證")
        endpoint = j.get("endpoint") or OSS_ENDPOINT
        return {"endpoint": endpoint, "AccessKeyId": j["AccessKeyId"], "AccessKeySecret": j["AccessKeySecret"],
                "SecurityToken": j["SecurityToken"]}

    def _locate(self, cid: str, name: str, sha1: str) -> CloudEntry | None:
        cands = [e for e in self.list_dir(cid) if not e.is_dir and e.sha1 == sha1.upper()]
        named = [e for e in cands if nfc(e.name) == nfc(name)] or cands
        return max(named, key=lambda e: e.mtime) if named else None

    def _result(self, cid: str, name: str, h: FileHashes, instant: bool, d: dict) -> UploadResult:
        fid = str(d.get("file_id") or d.get("fid") or "")
        pick = str(d.get("pick_code") or d.get("pickcode") or "")
        if not fid:
            e = self._locate(cid, name, h.sha1)
            if e is None:
                raise CloudError(f"上傳後在 115 找不到檔案：{name}")
            fid, pick = e.fid, e.pick_code
        return UploadResult(fid=fid, name=name, sha1=h.sha1, size=h.size, instant=instant, pick_code=pick)

    def try_instant(self, local_path: str, cid: str, name: str, hashes: FileHashes) -> UploadResult | None:
        d = self._init_with_sign_check(local_path, cid, name, hashes)
        if _int(d.get("status")) == STATUS_INSTANT:
            return self._result(cid, name, hashes, True, d)
        return None

    @staticmethod
    def _callback(d: dict) -> tuple[str, str]:
        cb = d.get("callback")
        if isinstance(cb, str):
            try:
                cb = json.loads(cb)
            except ValueError:
                return cb, ""
        cb = _first(cb)
        return str(cb.get("callback") or ""), str(cb.get("callback_var") or "")

    def upload(self, local_path: str, cid: str, name: str, hashes: FileHashes,
               control: TransferControl | None = None, sessions: SessionStore | None = None) -> UploadResult:
        from .oss_upload import upload_to_oss

        sessions = sessions or SessionStore()
        key = f"115web|{cid}|{hashes.sha1}|{hashes.size}"
        d = self._init_with_sign_check(local_path, cid, name, hashes)
        status = _int(d.get("status"))
        if status == STATUS_INSTANT:
            sessions.delete(key)
            return self._result(cid, name, hashes, True, d)
        if status != STATUS_NEED_UPLOAD or not d.get("bucket") or not d.get("object"):
            msg = d.get("statusmsg") or d.get("message") or d.get("error") or ""
            raise CloudError(f"115 無法開始上傳（status={status}）{msg}")
        bucket, obj = str(d["bucket"]), str(d["object"])
        callback, callback_var = self._callback(d)
        upload_id = None
        sess = sessions.get(key)
        if (sess and sess.get("upload_id") and sess.get("part_size") == self.part_size
                and hashes.size > self.part_size and sess.get("callback")):
            bucket, obj, upload_id = sess["bucket"], sess["object"], sess["upload_id"]
            callback, callback_var = sess["callback"], sess.get("callback_var", "")

        def remember(uid: str) -> None:
            sessions.put(key, {"bucket": bucket, "object": obj, "upload_id": uid, "part_size": self.part_size,
                               "callback": callback, "callback_var": callback_var, "name": name})

        try:
            body = upload_to_oss(local_path, hashes.size, self._oss_token, bucket, obj, callback, callback_var,
                                 self.part_size, control, upload_id, remember,
                                 getattr(self.session, "proxies", None) or None)
        except CloudError:
            if upload_id:  # 續傳資訊可能已過期：下次從頭上傳
                sessions.delete(key)
            raise
        if body and (body.get("Code") or not _ok(body)):  # "Code" = OSS 端錯誤（如 CallbackFailed）
            sessions.delete(key)
            raise CloudError(f"115 上傳回呼失敗：{_err(body)[1] or body.get('Code')}")
        sessions.delete(key)
        return self._result(cid, name, hashes, False, _first(body.get("data")) if body else {})

    # ------------------------------------------------------------ 下載
    def download_url(self, pick_code: str) -> str:
        try:
            j = self._call("POST", f"{PROAPI}/app/chrome/downurl", params={"t": int(time.time())},
                           data={"data": codec.m115_encode_json({"pickcode": pick_code})})
            info = j.get("data")
            if isinstance(info, str):
                info = json.loads(codec.m115_decode(info))
            for item in (info.values() if isinstance(info, dict) else []):
                url = item.get("url") if isinstance(item, dict) else None
                url = url.get("url") if isinstance(url, dict) else url
                if url:
                    return str(url)
        except AuthError:
            raise
        except (CloudError, ValueError) as e:
            log.info("取得下載網址失敗，改用網頁版介面：%s", e)
        j = self._call("POST", f"{WEBAPI}/files/download", data={"pickcode": pick_code})
        url = j.get("file_url") or _first(j.get("data")).get("file_url")
        if not url:
            raise CloudError("115 未回傳下載網址（檔案可能已刪除或是資料夾）")
        return str(url)

    def download(self, fid: str, pick_code: str, dest_path: str, control: TransferControl | None = None) -> None:
        url = self.download_url(pick_code)
        lp = long_path(dest_path)
        offset = os.path.getsize(lp) if os.path.exists(lp) else 0
        headers = {"User-Agent": USER_AGENT}  # 115 要求下載時的 UA 與取得網址時相同
        if offset:
            headers["Range"] = f"bytes={offset}-"
        try:
            with self.session.get(url, headers=headers, stream=True, timeout=self.timeout) as r:
                if r.status_code == 416:  # 已下載完
                    return
                if r.status_code not in (200, 206):
                    raise CloudError(f"下載失敗 HTTP {r.status_code}", r.status_code)
                if offset and r.status_code == 200:  # 伺服器不支援續傳：從頭開始
                    offset = 0
                with open(lp, "ab" if offset else "wb") as f:
                    if control is not None:
                        control.set_done(offset)
                    for chunk in r.iter_content(1024 * 1024):
                        if not chunk:
                            continue
                        if control is not None:
                            control.advance(len(chunk))
                        f.write(chunk)
        except requests.RequestException as e:
            raise CloudError(f"下載中斷（可續傳）：{e}") from e

    def set_proxy(self, proxy: str) -> None:
        self.session.proxies = {"http": proxy, "https": proxy} if proxy else {}
