import base64
import hashlib
import json

import pytest

from nas115sync.cloud import open115
from nas115sync.cloud.base import NotFoundError
from nas115sync.cloud.open115 import Open115Client, TokenStore
from nas115sync.hashing import hash_file, sha1_range


class Resp:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self.payload


class FakeSession:
    """依 (method, path) 路由的假 HTTP session，記錄所有請求。"""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.headers = {}

    def request(self, method, url, params=None, data=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "data": data, "headers": headers})
        handler = self.routes[(method, url.split("115.com", 1)[1])]
        return handler(params or {}, data or {}, headers or {})

    def get(self, url, params=None, timeout=None):
        return self.request("GET", url, params=params)

    def post(self, url, data=None, timeout=None):
        return self.request("POST", url, data=data)


def ok(data, **extra):
    return Resp({"state": True, "code": 0, "message": "", "data": data, **extra})


def make_client(tmp_path, routes, tokens=None):
    store = TokenStore(tmp_path / "tokens.json")
    store.save(tokens or {"access_token": "AT", "refresh_token": "RT", "expires_at": 9e12})
    return Open115Client(store, client_id="100195", min_interval=0, session=FakeSession(routes))


def test_qr_login_pkce(tmp_path):
    seen = {}

    def auth(params, data, headers):
        seen.update(data)
        return Resp({"state": 1, "code": 0, "data": {"uid": "U1", "time": 1700, "qrcode": "https://115.com/scan/x",
                                                       "sign": "S"}})

    def token(params, data, headers):
        seen["verifier_sent"] = data["code_verifier"]
        return Resp({"state": 1, "code": 0, "data": {"access_token": "NEWAT", "refresh_token": "NEWRT",
                                                       "expires_in": 7200}})

    c = make_client(tmp_path, {
        ("POST", "/open/authDeviceCode"): auth,
        ("GET", "/get/status/"): lambda p, d, h: Resp({"state": 1, "data": {"status": 2}}),
        ("POST", "/open/deviceCodeToToken"): token,
    }, tokens={})
    login = c.start_qr_login()
    assert seen["client_id"] == "100195" and seen["code_challenge_method"] == "sha256"
    expected = base64.b64encode(hashlib.sha256(login.code_verifier.encode()).digest()).decode()
    assert seen["code_challenge"] == expected
    assert 43 <= len(login.code_verifier) <= 128
    assert c.poll_qr_status(login) == open115.QR_CONFIRMED
    c.finish_qr_login(login)
    assert seen["verifier_sent"] == login.code_verifier
    assert TokenStore(tmp_path / "tokens.json").load()["refresh_token"] == "NEWRT"


def test_list_dir_paginates_and_parses(tmp_path, monkeypatch):
    monkeypatch.setattr(open115, "PAGE_LIMIT", 2)
    items = [
        {"fid": "11", "pid": "5", "fc": "0", "fn": "子資料夾"},
        {"fid": "12", "pid": "5", "fc": "1", "fn": "a.txt", "fs": 3, "sha1": "abc", "pc": "p12", "upt": 5},
        {"fid": "13", "pid": "5", "fc": "1", "fn": "b.txt", "fs": "4", "sha1": "DEF", "pc": "p13"},
    ]

    def files(params, data, headers):
        assert headers["Authorization"] == "Bearer AT"
        off, lim = int(params["offset"]), int(params["limit"])
        return Resp({"state": True, "code": 0, "count": 3, "cid": params["cid"], "data": items[off:off + lim]})

    c = make_client(tmp_path, {("GET", "/open/ufile/files"): files})
    entries = c.list_dir("5")
    assert [e.name for e in entries] == ["子資料夾", "a.txt", "b.txt"]
    assert entries[0].is_dir and not entries[1].is_dir
    assert entries[1].sha1 == "ABC" and entries[2].size == 4 and entries[1].pick_code == "p12"


def test_list_dir_detects_silent_root_fallback(tmp_path):
    c = make_client(tmp_path, {("GET", "/open/ufile/files"):
                               lambda p, d, h: Resp({"state": True, "count": 0, "cid": 0, "data": []})})
    with pytest.raises(NotFoundError):
        c.list_dir("999")


def test_expired_token_is_refreshed_once(tmp_path):
    calls = {"n": 0}

    def info(params, data, headers):
        calls["n"] += 1
        if headers["Authorization"] == "Bearer AT":
            return Resp({"state": False, "code": 40140125, "message": "access_token 已过期"})
        return ok({"user_name": "測試用戶"})

    c = make_client(tmp_path, {
        ("GET", "/open/user/info"): info,
        ("POST", "/open/refreshToken"): lambda p, d, h: Resp(
            {"state": 1, "code": 0, "data": {"access_token": "AT2", "refresh_token": "RT2", "expires_in": 7200}}),
    })
    assert c.user_label() == "測試用戶"
    assert calls["n"] == 2
    assert TokenStore(tmp_path / "tokens.json").load()["refresh_token"] == "RT2"


def test_upload_instant_with_sign_check(tmp_path):
    f = tmp_path / "big.bin"
    f.write_bytes(bytes(range(256)) * 2000)
    h = hash_file(str(f))
    inits = []

    def init(params, data, headers):
        inits.append(dict(data))
        if "sign_key" not in data:
            return ok({"status": 7, "sign_key": "K1", "sign_check": "100-299"})
        return ok({"status": 2, "file_id": "F1", "pick_code": "PC1"})

    c = make_client(tmp_path, {("POST", "/open/upload/init"): init})
    res = c.try_instant(str(f), "42", "big.bin", h)
    assert res.instant and res.fid == "F1"
    first, second = inits
    assert first["target"] == "U_1_42" and first["fileid"] == h.sha1 and first["preid"] == h.preid
    assert first["file_size"] == str(h.size)
    assert second["sign_key"] == "K1"
    assert second["sign_val"] == sha1_range(str(f), 100, 299)
    assert second["sign_val"] == hashlib.sha1(f.read_bytes()[100:300]).hexdigest().upper()


def test_upload_falls_back_to_oss(tmp_path, monkeypatch):
    f = tmp_path / "new.bin"
    f.write_bytes(b"brand new content")
    h = hash_file(str(f))
    captured = {}

    def fake_oss(local_path, size, token_provider, bucket, obj, callback, callback_var, part_size, progress, cancel):
        captured.update(bucket=bucket, obj=obj, callback=callback, callback_var=callback_var,
                        token=token_provider())
        return {"state": True, "code": 0, "data": {"file_id": "F9", "pick_code": "P9"}}

    monkeypatch.setattr("nas115sync.cloud.oss_upload.upload_to_oss", fake_oss)
    c = make_client(tmp_path, {
        ("POST", "/open/upload/init"): lambda p, d, hd: ok({
            "status": 1, "bucket": "fhnfile", "object": "obj/key", "pick_code": "P9",
            "callback": {"callback": '{"cb":1}', "callback_var": '{"x:var":1}'}}),
        ("GET", "/open/upload/get_token"): lambda p, d, hd: ok({
            "endpoint": "http://oss-cn-shenzhen.aliyuncs.com", "AccessKeyId": "AK", "AccessKeySecret": "SK",
            "SecurityToken": "ST", "expiration": "2030-01-01"}),
    })
    res = c.upload(str(f), "42", "new.bin", h)
    assert not res.instant and res.fid == "F9"
    assert captured["bucket"] == "fhnfile" and captured["obj"] == "obj/key"
    assert captured["callback"] == '{"cb":1}' and captured["callback_var"] == '{"x:var":1}'
    assert captured["token"]["AccessKeyId"] == "AK"


def test_resolve_path_validates_get_info(tmp_path):
    def get_info(params, data, headers):
        # 回傳不相符的資料夾（例如 API 忽略 path 參數）→ 必須改為逐層查找
        return ok({"file_id": "0", "file_name": "根目录", "file_category": "0"})

    def files(params, data, headers):
        tree = {"0": [{"fid": "7", "fc": "0", "fn": "備份"}], "7": [{"fid": "8", "fc": "0", "fn": "NAS"}]}
        return Resp({"state": True, "count": len(tree.get(params["cid"], [])), "cid": params["cid"],
                     "data": tree.get(params["cid"], [])})

    c = make_client(tmp_path, {("POST", "/open/folder/get_info"): get_info, ("GET", "/open/ufile/files"): files})
    assert c.resolve_path("/備份/NAS") == "8"
    assert c.resolve_path("/備份/不存在") is None


def test_endpoint_upgraded_to_https():
    from nas115sync.cloud.oss_upload import _endpoint

    assert _endpoint("http://oss-cn-shenzhen.aliyuncs.com") == "https://oss-cn-shenzhen.aliyuncs.com"
