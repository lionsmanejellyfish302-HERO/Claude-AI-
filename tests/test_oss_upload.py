import base64
import json

import oss2

from nas115sync.cloud import oss_upload


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
        self.put = (key, data, headers)
        return FakeResult(json.dumps({"state": True, "data": {"file_id": "F1"}}).encode())

    def init_multipart_upload(self, key):
        class R:
            upload_id = "UP1"
        return R()

    def upload_part(self, key, upload_id, n, data):
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
    seen = []
    body = oss_upload.upload_to_oss(str(f), 2500, token, "fhnfile", "obj", "{}", "", 1000,
                                    progress=lambda d, t: seen.append(d))
    b = FakeBucket.instances[-1]
    assert b.parts == [(1, 1000), (2, 1000), (3, 500)]
    nums, headers = b.completed
    assert nums == [1, 2, 3] and "x-oss-callback" in headers and "x-oss-callback-var" not in headers
    assert seen == [1000, 2000, 2500]
    assert body["data"]["file_id"] == "F2"
