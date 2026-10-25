"""115 開放平台（open.115.com）客戶端。

- 登入：裝置碼 + PKCE，以 115 App 掃 QR 碼授權；或直接貼上 refresh_token
- 上傳：upload/init 帶整檔 SHA-1（fileid）與前 128KB SHA-1（preid）→ 115 端已有相同檔案即「秒傳」
  （status=2）；若要求二次驗證（status 6/7/8 + sign_check）則計算指定區段 SHA-1 後重送；
  仍無法秒傳才取得 OSS 憑證實際上傳。這和 115 瀏覽器／客戶端觸發秒傳的機制相同。
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import logging
import os
import secrets
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from .. import __version__
from ..hashing import FileHashes, sha1_range
from ..paths import cloud_norm, cloud_parts, long_path, nfc
from ..transfers import TransferControl
from .base import AuthError, CloudClient, CloudEntry, CloudError, NotFoundError, SessionStore, UploadResult

log = logging.getLogger(__name__)

API_BASE = "https://proapi.115.com"
PASSPORT_BASE = "https://passportapi.115.com"
QR_STATUS_URL = "https://qrcodeapi.115.com/get/status/"
USER_AGENT = f"NAS115Sync/{__version__}"
PAGE_LIMIT = 1150

QR_WAITING, QR_SCANNED, QR_CONFIRMED = 0, 1, 2
SIGN_CHECK_STATUSES = {6, 7, 8}
STATUS_NEED_UPLOAD, STATUS_INSTANT = 1, 2


# --------------------------------------------------------------------------- Token 保存
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    """Windows DPAPI：token 以目前 Windows 使用者身分加密，別的帳號或電腦無法解開。"""
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise ctypes.WinError()  # type: ignore[attr-defined]
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


class TokenStore:
    PREFIX = b"DPAPI:"

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        raw = self.path.read_bytes()
        try:
            if raw.startswith(self.PREFIX):
                raw = _dpapi(base64.b64decode(raw[len(self.PREFIX):]), protect=False)
            return json.loads(raw.decode("utf-8"))
        except (OSError, ValueError) as e:
            log.warning("無法讀取 115 登入資訊（需重新登入）：%s", e)
            return {}

    def save(self, data: dict) -> None:
        raw = json.dumps(data).encode("utf-8")
        if sys.platform == "win32":
            try:
                raw = self.PREFIX + base64.b64encode(_dpapi(raw, protect=True))
            except OSError as e:
                log.warning("DPAPI 加密失敗，token 以明文保存：%s", e)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(raw)
        os.replace(tmp, self.path)


@dataclass
class QrLogin:
    uid: str
    time: int
    sign: str
    qrcode: str  # 要編成 QR 碼給 115 App 掃描的內容
    code_verifier: str


# --------------------------------------------------------------------------- 工具
def _first(d):
    if isinstance(d, list):
        return d[0] if d else {}
    return d or {}


def _is_ok(j: dict) -> bool:
    state = j.get("state")
    if state is None:
        return int(j.get("code") or 0) == 0
    return bool(state)


def _is_auth_code(code) -> bool:
    return str(code) == "99" or str(code).startswith("401")


def _is_rate_limited(code, msg: str) -> bool:
    msg = msg or ""
    return str(code) in {"770004", "406"} or any(k in msg for k in ("频繁", "頻繁", "上限", "稍后", "稍後", "limit"))


def parse_entry(it: dict) -> CloudEntry:
    fc = str(it.get("fc", it.get("file_category", "1")))
    return CloudEntry(
        fid=str(it.get("fid") or it.get("file_id") or ""),
        name=nfc(str(it.get("fn") or it.get("file_name") or "")),
        is_dir=fc == "0",
        pid=str(it.get("pid") or it.get("parent_id") or ""),
        size=int(it.get("fs") or it.get("size_byte") or it.get("file_size") or 0),
        sha1=str(it.get("sha1") or "").upper(),
        pick_code=str(it.get("pc") or it.get("pick_code") or ""),
        mtime=int(it.get("upt") or it.get("uet") or 0),
    )


# --------------------------------------------------------------------------- 客戶端
class Open115Client(CloudClient):
    def __init__(self, token_store: TokenStore, client_id: str = "", min_interval: float = 0.4,
                 part_size: int = 20 * 1024 * 1024, timeout: float = 60, session: requests.Session | None = None):
        super().__init__()
        self.tokens = token_store
        self.client_id = client_id
        self.min_interval = min_interval
        self.part_size = part_size
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self._tok = token_store.load()
        self._rate_lock = threading.Lock()
        self._auth_lock = threading.RLock()
        self._last_call = 0.0

    # ------------------------------------------------------------ 登入
    def is_logged_in(self) -> bool:
        return bool(self._tok.get("refresh_token") or self._tok.get("access_token"))

    def start_qr_login(self) -> QrLogin:
        if not self.client_id:
            raise AuthError("需先在設定填入 115 開放平台 AppID（client_id）")
        verifier = secrets.token_urlsafe(64)[:64]
        challenge = base64.b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii")
        d = self._passport("/open/authDeviceCode", {
            "client_id": self.client_id,
            "code_challenge": challenge,
            "code_challenge_method": "sha256",
        })
        return QrLogin(str(d["uid"]), int(d["time"]), str(d["sign"]), str(d["qrcode"]), verifier)

    def poll_qr_status(self, login: QrLogin, timeout: float = 35) -> int:
        """回傳 0=等待掃描 1=已掃描待確認 2=已確認；負數=過期或取消。"""
        try:
            r = self.session.get(QR_STATUS_URL, params={"uid": login.uid, "time": login.time, "sign": login.sign},
                                 timeout=timeout)
            j = r.json()
        except requests.Timeout:
            return QR_WAITING
        except (requests.RequestException, ValueError) as e:
            raise AuthError(f"查詢 QR 碼狀態失敗：{e}") from e
        d = j.get("data") if isinstance(j.get("data"), dict) else j
        return int(d.get("status") or 0)

    def finish_qr_login(self, login: QrLogin) -> None:
        d = self._passport("/open/deviceCodeToToken", {"uid": login.uid, "code_verifier": login.code_verifier})
        self._store_tokens(d)

    def login_with_refresh_token(self, refresh_token: str) -> None:
        self._tok = {"refresh_token": refresh_token.strip()}
        self.refresh()

    def logout(self) -> None:
        self._tok = {}
        self.tokens.save({})

    def refresh(self) -> None:
        with self._auth_lock:
            rt = self._tok.get("refresh_token")
            if not rt:
                raise AuthError("尚未登入 115（沒有 refresh_token）")
            d = self._passport("/open/refreshToken", {"refresh_token": rt})
            self._store_tokens(d)
            log.info("115 access_token 已更新")

    def _store_tokens(self, d: dict) -> None:
        if not d.get("access_token"):
            raise AuthError(f"115 未回傳 access_token：{d}")
        self._tok = {
            "access_token": d["access_token"],
            "refresh_token": d.get("refresh_token") or self._tok.get("refresh_token", ""),
            "expires_at": time.time() + int(d.get("expires_in") or 7200),
        }
        self.tokens.save(self._tok)

    def _token_expiring(self) -> bool:
        exp = float(self._tok.get("expires_at") or 0)
        return bool(self._tok.get("refresh_token")) and (not self._tok.get("access_token") or exp - 300 < time.time())

    def _ensure_fresh(self) -> None:
        if self._token_expiring():
            with self._auth_lock:
                if self._token_expiring():  # 其他執行緒可能已更新
                    self.refresh()

    def _passport(self, path: str, form: dict) -> dict:
        try:
            r = self.session.post(PASSPORT_BASE + path, data=form, timeout=self.timeout)
            j = r.json()
        except (requests.RequestException, ValueError) as e:
            raise AuthError(f"115 登入服務連線失敗：{e}") from e
        code = int(j.get("code") or 0)
        if code != 0 or j.get("state") in (0, False) or j.get("error"):
            raise AuthError(j.get("message") or j.get("error") or "115 登入失敗", code or j.get("errno"))
        return _first(j.get("data"))

    # ------------------------------------------------------------ API 共用
    def _throttle(self) -> None:
        with self._rate_lock:
            wait = self._last_call + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    def _api(self, method: str, path: str, params: dict | None = None, data: dict | None = None,
             _auth_retried: bool = False) -> dict:
        if not self.is_logged_in():
            raise AuthError("尚未登入 115")
        self._ensure_fresh()
        url = path if path.startswith("http") else API_BASE + path
        for attempt in range(4):
            self._throttle()
            try:
                r = self.session.request(method, url, params=params, data=data, timeout=self.timeout,
                                         headers={"Authorization": f"Bearer {self._tok.get('access_token', '')}"})
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
            if r.status_code == 401 and not _auth_retried:
                self.refresh()
                return self._api(method, path, params, data, _auth_retried=True)
            try:
                j = r.json()
            except ValueError as e:
                raise CloudError(f"115 回應不是 JSON：HTTP {r.status_code} {r.text[:200]}") from e
            if _is_ok(j):
                return j
            code, msg = j.get("code"), str(j.get("message") or j.get("error") or "")
            if _is_auth_code(code) and not _auth_retried:
                self.refresh()
                return self._api(method, path, params, data, _auth_retried=True)
            if _is_rate_limited(code, msg) and attempt < 3:
                log.warning("115 API 限流（%s），稍候重試", msg or code)
                time.sleep(5 * (attempt + 1))
                continue
            if str(code) == "430004":
                raise NotFoundError(msg or "檔案不存在", code)
            raise CloudError(msg or json.dumps(j, ensure_ascii=False)[:300], code)
        raise CloudError("115 API 多次重試失敗")

    # ------------------------------------------------------------ 檔案操作
    def user_label(self) -> str:
        d = _first(self._api("GET", "/open/user/info").get("data"))
        return str(d.get("user_name") or d.get("user_id") or "")

    def list_dir(self, cid: str) -> list[CloudEntry]:
        out: list[CloudEntry] = []
        seen: set[str] = set()
        offset = 0
        while True:
            j = self._api("GET", "/open/ufile/files", params={
                "cid": cid, "limit": PAGE_LIMIT, "offset": offset,
                "show_dir": 1, "cur": 1, "asc": 1, "o": "file_name",
            })
            # 115 對不存在的 cid 會靜默回傳根目錄，必須檢查
            if j.get("cid") is not None and str(j.get("cid")) != str(cid):
                raise NotFoundError(f"115 資料夾不存在（cid={cid}）")
            items = [parse_entry(it) for it in (j.get("data") or []) if isinstance(it, dict)]
            new = [e for e in items if e.fid not in seen]
            if not new:
                break
            seen.update(e.fid for e in new)
            out.extend(new)
            offset += len(items)
            if offset >= int(j.get("count") or 0) and len(items) < PAGE_LIMIT:
                break
        return out

    def get_entry(self, fid: str) -> CloudEntry | None:
        try:
            d = _first(self._api("GET", "/open/folder/get_info", params={"file_id": fid}).get("data"))
        except NotFoundError:
            return None
        return parse_entry(d) if d else None

    def resolve_path(self, path: str) -> str | None:
        parts = cloud_parts(path)
        if not parts:
            return self.root_cid
        try:
            d = _first(self._api("POST", "/open/folder/get_info", data={"path": cloud_norm(path)}).get("data"))
            ancestors = [nfc(p.get("file_name", "")) for p in d.get("paths") or [] if str(p.get("file_id")) != "0"]
            if (d.get("file_id") and str(d.get("file_category")) == "0"
                    and nfc(d.get("file_name", "")) == nfc(parts[-1])
                    and (not d.get("paths") or ancestors == [nfc(p) for p in parts[:-1]])):
                return str(d["file_id"])
        except CloudError as e:
            log.debug("以路徑查詢 115 資料夾失敗，改為逐層查找：%s", e)
        return super().resolve_path(path)

    def mkdir(self, pid: str, name: str) -> str:
        d = _first(self._api("POST", "/open/folder/add", data={"pid": pid, "file_name": name}).get("data"))
        if not d.get("file_id"):
            raise CloudError(f"建立 115 資料夾失敗：{name}")
        return str(d["file_id"])

    def delete(self, fids: list[str], parent_id: str = "") -> None:
        if not fids:
            return
        data = {"file_ids": ",".join(fids)}
        if parent_id:
            data["parent_id"] = parent_id
        self._api("POST", "/open/ufile/delete", data=data)

    def rename(self, fid: str, new_name: str) -> None:
        self._api("POST", "/open/ufile/update", data={"file_id": fid, "file_name": new_name})

    def move(self, fids: list[str], to_cid: str) -> None:
        if fids:
            self._api("POST", "/open/ufile/move", data={"file_ids": ",".join(fids), "to_cid": to_cid})

    # ------------------------------------------------------------ 上傳／秒傳
    def _init_upload(self, cid: str, name: str, h: FileHashes, sign_key: str = "", sign_val: str = "") -> dict:
        data = {
            "file_name": name,
            "file_size": str(h.size),
            "target": f"U_1_{cid}",
            "fileid": h.sha1.upper(),
            "preid": h.preid.upper(),
            "topupload": "0",
        }
        if sign_key:
            data["sign_key"] = sign_key
            data["sign_val"] = sign_val.upper()
        return _first(self._api("POST", "/open/upload/init", data=data).get("data"))

    def _init_with_sign_check(self, local_path: str, cid: str, name: str, h: FileHashes) -> dict:
        d = self._init_upload(cid, name, h)
        for _ in range(3):
            if int(d.get("status") or 0) not in SIGN_CHECK_STATUSES or not d.get("sign_check"):
                break
            start, _, end = str(d["sign_check"]).partition("-")
            val = sha1_range(local_path, int(start), int(end))
            log.debug("115 二次驗證 %s 區段 %s", name, d["sign_check"])
            d = self._init_upload(cid, name, h, sign_key=str(d.get("sign_key") or ""), sign_val=val)
        return d

    def _upload_token(self) -> dict:
        d = _first(self._api("GET", "/open/upload/get_token").get("data"))
        return {
            "endpoint": d.get("endpoint", ""),
            "AccessKeyId": d.get("AccessKeyId", ""),
            "AccessKeySecret": d.get("AccessKeySecret", ""),
            "SecurityToken": d.get("SecurityToken", ""),
        }

    def _locate(self, cid: str, name: str, sha1: str) -> CloudEntry | None:
        cands = [e for e in self.list_dir(cid) if not e.is_dir and e.sha1 == sha1.upper()]
        named = [e for e in cands if nfc(e.name) == nfc(name)] or cands
        return max(named, key=lambda e: e.mtime) if named else None

    def _result(self, cid: str, name: str, h: FileHashes, instant: bool, d: dict) -> UploadResult:
        fid = str(d.get("file_id") or "")
        pick = str(d.get("pick_code") or "")
        if not fid:
            e = self._locate(cid, name, h.sha1)
            if e is None:
                raise CloudError(f"上傳後在 115 找不到檔案：{name}")
            fid, pick = e.fid, e.pick_code
        return UploadResult(fid=fid, name=name, sha1=h.sha1, size=h.size, instant=instant, pick_code=pick)

    def try_instant(self, local_path: str, cid: str, name: str, hashes: FileHashes) -> UploadResult | None:
        d = self._init_with_sign_check(local_path, cid, name, hashes)
        if int(d.get("status") or 0) == STATUS_INSTANT:
            return self._result(cid, name, hashes, True, d)
        return None

    @staticmethod
    def _callback(d: dict) -> tuple[str, str]:
        cb = _first(d.get("callback"))
        if "value" in cb and isinstance(cb["value"], dict):
            cb = cb["value"]
        return str(cb.get("callback") or ""), str(cb.get("callback_var") or "")

    def _resume_info(self, cid: str, h: FileHashes, pick_code: str) -> dict:
        """115 斷點續傳：以先前的 pick_code 取回同一個上傳工作的 bucket/object/callback。"""
        return _first(self._api("POST", "/open/upload/resume", data={
            "file_size": str(h.size), "target": f"U_1_{cid}", "fileid": h.sha1.upper(), "pick_code": pick_code,
        }).get("data"))

    def upload(self, local_path: str, cid: str, name: str, hashes: FileHashes,
               control: TransferControl | None = None, sessions: SessionStore | None = None) -> UploadResult:
        from .oss_upload import upload_to_oss

        sessions = sessions or SessionStore()
        d = self._init_with_sign_check(local_path, cid, name, hashes)
        status = int(d.get("status") or 0)
        key = f"115|{cid}|{hashes.sha1}|{hashes.size}"
        if status == STATUS_INSTANT:
            sessions.delete(key)
            return self._result(cid, name, hashes, True, d)
        if status != STATUS_NEED_UPLOAD or not d.get("bucket") or not d.get("object"):
            raise CloudError(f"115 upload/init 回傳未預期狀態 status={status}：{json.dumps(d, ensure_ascii=False)[:300]}")

        bucket, obj = str(d["bucket"]), str(d["object"])
        callback, callback_var = self._callback(d)
        upload_id = None
        sess = sessions.get(key)
        if sess and sess.get("upload_id") and sess.get("part_size") == self.part_size and hashes.size > self.part_size:
            try:
                r = self._resume_info(cid, hashes, sess["pick_code"])
                if str(r.get("object") or "") == sess.get("object"):
                    bucket, obj = str(r.get("bucket") or sess["bucket"]), sess["object"]
                    callback, callback_var = self._callback(r) if r.get("callback") else (callback, callback_var)
                    upload_id = sess["upload_id"]
            except CloudError as e:
                log.info("115 續傳資訊無效，重新上傳 %s：%s", name, e)

        def remember(uid: str) -> None:
            sessions.put(key, {"pick_code": str(d.get("pick_code") or ""), "bucket": bucket, "object": obj,
                               "upload_id": uid, "part_size": self.part_size, "name": name})

        body = upload_to_oss(local_path, hashes.size, self._upload_token, bucket, obj, callback, callback_var,
                             self.part_size, control, upload_id, remember, getattr(self.session, "proxies", None) or None)
        if body and (body.get("Code") or not _is_ok(body)):  # "Code" = OSS 端錯誤（如 CallbackFailed）
            raise CloudError(f"115 上傳回呼失敗：{json.dumps(body, ensure_ascii=False)[:300]}")
        sessions.delete(key)
        return self._result(cid, name, hashes, False, _first(body.get("data")) if body else {})

    # ------------------------------------------------------------ 下載／搜尋
    def download_url(self, pick_code: str) -> str:
        d = self._api("POST", "/open/ufile/downurl", data={"pick_code": pick_code}).get("data") or {}
        info = next(iter(d.values()), {}) if isinstance(d, dict) else _first(d)
        url = info.get("url")
        url = url.get("url") if isinstance(url, dict) else url
        if not url:
            raise CloudError(f"115 未回傳下載網址：{json.dumps(d, ensure_ascii=False)[:200]}")
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

    def search(self, keyword: str, cid: str = "0", limit: int = 500) -> list[CloudEntry]:
        out: list[CloudEntry] = []
        offset = 0
        while len(out) < limit:
            j = self._api("GET", "/open/ufile/search", params={
                "search_value": keyword, "cid": cid, "limit": min(100, limit - len(out)), "offset": offset})
            items = [parse_entry(it) for it in (j.get("data") or []) if isinstance(it, dict)]
            if not items:
                break
            out += items
            offset += len(items)
            if offset >= int(j.get("count") or 0):
                break
        return out

    def set_proxy(self, proxy: str) -> None:
        self.session.proxies = {"http": proxy, "https": proxy} if proxy else {}
