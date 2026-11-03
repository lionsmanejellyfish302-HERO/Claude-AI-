import base64
import json

import oss2
import pytest

from nas115sync.cloud import oss_upload
from nas115sync.transfers import Cancelled, TransferManager


class FakeResult:
    def __init__(self, body=b"", etag="E", crc=None):
        self.etag = etag
        self.crc = crc
        self.resp = self
        self._body = body

    def read(self):
        return self._body


class FakeBucket:
    instances = []

    def __init__(self, auth, endpoint, bucket_name):
        self.endpoint, self.bucket_name = endpoint, bucket_name
        self.parts, self.completed, self.put = [], None, None
        FakeBucket.instances.append(self)

    def put_object(self, key, data, headers=None, progress_callback=None):
        if progress_callback:
            progress_callback(len(data), len(data))
        self.put = (key, data, headers)
        return FakeResult(json.dumps({"state": True, "data": {"file_id": "F1"}}).encode())

    def init_multipart_upload(self, key):
        class R:
            upload_id = "UP1"
        return R()

    def upload_part(self, key, upload_id, n, data, progress_callback=None):
        if progress_callback:
            progress_callback(len(data), len(data))
        self.parts.append((n, len(data)))
        return FakeResult(etag=f"E{n}")

    def complete_multipart_upload(self, key, upload_id, parts, headers=None):
        self.completed = ([p.part_number for p in parts], headers)
        return FakeResult(json.dumps({"state": True, "data": {"file_id": "F2"}}).encode())

    def abort_multipart_upload(self, key, upload_id):
        pass


def token():
    return {"endpoint": "http://oss-cn-shenzhen.aliyuncs.com", "AccessKeyId": "AK",
            "AccessKeySecret": "SK", "SecurityToken": "ST"}


def test_single_put_sends_callback(tmp_path, monkeypatch):
    monkeypatch.setattr(oss2, "Bucket", FakeBucket)
    f = tmp_path / "a.bin"
    f.write_bytes(b"x" * 100)
    body = oss_upload.upload_to_oss(str(f), 100, token, "fhnfile", "obj", '{"cb":1}', '{"v":2}', 1024)
    b = FakeBucket.instances[-1]
    assert b.endpoint == "https://oss-cn-shenzhen.aliyuncs.com" and b.bucket_name == "fhnfile"
    key, data, headers = b.put
    assert key == "obj" and data == b"x" * 100
    assert base64.b64decode(headers["x-oss-callback"]) == b'{"cb":1}'
    assert base64.b64decode(headers["x-oss-callback-var"]) == b'{"v":2}'
    assert body["data"]["file_id"] == "F1"


def test_multipart_upload(tmp_path, monkeypatch):
    monkeypatch.setattr(oss2, "Bucket", FakeBucket)
    f = tmp_path / "big.bin"
    f.write_bytes(b"y" * 2500)
    mgr = TransferManager()
    ctrl = mgr.start("upload", "m", "big.bin", 2500)
    ids = []
    body = oss_upload.upload_to_oss(str(f), 2500, token, "fhnfile", "obj", "{}", "", 1000, ctrl,
                                    on_upload_id=ids.append)
    b = FakeBucket.instances[-1]
    assert b.parts == [(1, 1000), (2, 1000), (3, 500)]
    nums, headers = b.completed
    assert nums == [1, 2, 3] and "x-oss-callback" in headers and "x-oss-callback-var" not in headers
    assert ctrl.t.done == 2500 and ids == ["UP1"]
    assert body["data"]["file_id"] == "F2"


def test_multipart_resume_skips_finished_parts(tmp_path, monkeypatch):
    class ResumeBucket(FakeBucket):
        def list_parts(self, key, upload_id, marker="", max_parts=1000, headers=None):
            assert upload_id == "OLD"

            class R:
                parts = [oss2.models.PartInfo(1, "E1", size=1000), oss2.models.PartInfo(2, "E2", size=1000)]
                is_truncated = False
                next_marker = ""
            return R()

    monkeypatch.setattr(oss2, "Bucket", ResumeBucket)
    f = tmp_path / "big.bin"
    f.write_bytes(b"z" * 2500)
    oss_upload.upload_to_oss(str(f), 2500, token, "fhnfile", "obj", "{}", "", 1000, upload_id="OLD")
    b = FakeBucket.instances[-1]
    assert b.parts == [(3, 500)]  # 只補傳缺少的分片
    assert b.completed[0] == [1, 2, 3]


def test_cancel_stops_upload(tmp_path, monkeypatch):
    monkeypatch.setattr(oss2, "Bucket", FakeBucket)
    f = tmp_path / "big.bin"
    f.write_bytes(b"y" * 2500)
    mgr = TransferManager()
    ctrl = mgr.start("upload", "m", "big.bin", 2500)
    mgr.cancel(ctrl.t.id)
    with pytest.raises(Cancelled):
        oss_upload.upload_to_oss(str(f), 2500, token, "fhnfile", "obj", "{}", "", 1000, ctrl)


def test_network_errors_are_retried_with_fresh_credentials(tmp_path, monkeypatch):
    """網路中斷或 OSS 暫時錯誤：等一下用新的上傳憑證重試同一個分片，不必整個重來。"""
    failures = {2: [oss2.exceptions.RequestError(ConnectionResetError("reset")), OSError("NAS 讀取逾時")]}

    class FlakyBucket(FakeBucket):
        def upload_part(self, key, upload_id, n, data, progress_callback=None):
            if failures.get(n):
                raise failures[n].pop(0)
            return super().upload_part(key, upload_id, n, data, progress_callback)

    monkeypatch.setattr(oss2, "Bucket", FlakyBucket)
    monkeypatch.setattr(oss_upload.time, "sleep", lambda s: None)
    tokens, first = [], len(FakeBucket.instances)
    f = tmp_path / "big.bin"
    f.write_bytes(b"y" * 2500)
    body = oss_upload.upload_to_oss(str(f), 2500, lambda: tokens.append(1) or token(), "fhnfile", "obj", "{}", "",
                                    1000)
    assert sorted(n for b in FakeBucket.instances[first:] for n, _size in b.parts) == [1, 2, 3]
    assert len(tokens) == 3 and body["data"]["file_id"] == "F2"  # 每次重試都換新憑證


def test_gives_up_after_repeated_failures(tmp_path, monkeypatch):
    class DownBucket(FakeBucket):
        def put_object(self, *a, **kw):
            raise OSError("網路中斷")

    monkeypatch.setattr(oss2, "Bucket", DownBucket)
    monkeypatch.setattr(oss_upload.time, "sleep", lambda s: None)
    f = tmp_path / "a.bin"
    f.write_bytes(b"x" * 100)
    with pytest.raises(oss_upload.CloudError):
        oss_upload.upload_to_oss(str(f), 100, token, "fhnfile", "obj", "{}", "", 1024)


def test_expired_resume_id_restarts_upload(tmp_path, monkeypatch):
    """保存的分片上傳 ID 已過期（OSS 回 NoSuchUpload）：改為重新上傳，並保存新的 ID。"""
    class ExpiredBucket(FakeBucket):
        def list_parts(self, key, upload_id, marker="", max_parts=1000, headers=None):
            raise oss2.exceptions.NoSuchUpload(404, {}, b"", {"Code": "NoSuchUpload"})

    monkeypatch.setattr(oss2, "Bucket", ExpiredBucket)
    f = tmp_path / "big.bin"
    f.write_bytes(b"z" * 2500)
    ids = []
    oss_upload.upload_to_oss(str(f), 2500, token, "fhnfile", "obj", "{}", "", 1000, upload_id="OLD",
                             on_upload_id=ids.append)
    b = FakeBucket.instances[-1]
    assert ids == ["UP1"] and b.parts == [(1, 1000), (2, 1000), (3, 500)] and b.completed[0] == [1, 2, 3]
