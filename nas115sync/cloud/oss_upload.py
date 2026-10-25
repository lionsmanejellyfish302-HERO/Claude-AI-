"""115 非秒傳檔案的實際上傳：依 upload/init 回傳的 bucket/object 與 STS 憑證上傳到阿里雲 OSS，
並帶上 115 的 callback，讓 115 在上傳完成後建立檔案。

大檔分片上傳支援「斷點續傳」：分片上傳 ID 會透過 on_upload_id 交給呼叫端保存，
中斷後再次上傳同一檔案時，以 ListParts 找出已完成的分片，只補傳缺少的部分。
"""
from __future__ import annotations

import base64
import json
import logging
import time
from typing import Callable

from ..paths import long_path
from ..transfers import Cancelled, TransferControl
from .base import CloudError

log = logging.getLogger(__name__)

TOKEN_MAX_AGE = 40 * 60  # STS 憑證約 1 小時有效，提早更新


def _b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def _endpoint(ep: str) -> str:
    ep = (ep or "").strip()
    if not ep:
        raise CloudError("115 未回傳 OSS endpoint")
    if ep.startswith("http://") and ".aliyuncs.com" in ep:
        ep = "https://" + ep[len("http://"):]
    return ep


def upload_to_oss(
    local_path: str,
    size: int,
    token_provider: Callable[[], dict],
    bucket_name: str,
    object_key: str,
    callback: str,
    callback_var: str,
    part_size: int,
    control: TransferControl | None = None,
    upload_id: str | None = None,
    on_upload_id: Callable[[str], None] | None = None,
    proxies: dict | None = None,
) -> dict:
    """上傳並回傳 115 callback 的 JSON（可能為空 dict）。"""
    import oss2  # 延遲載入：測試與秒傳不需要

    state = {"token_at": 0.0, "bucket": None}

    def bucket(force: bool = False):
        if force or state["bucket"] is None or time.time() - state["token_at"] > TOKEN_MAX_AGE:
            tok = token_provider()
            auth = oss2.StsAuth(tok["AccessKeyId"], tok["AccessKeySecret"], tok["SecurityToken"])
            kwargs = {"proxies": proxies} if proxies else {}
            state["bucket"] = oss2.Bucket(auth, _endpoint(tok["endpoint"]), bucket_name, **kwargs)
            state["token_at"] = time.time()
        return state["bucket"]

    def check():
        if control is not None:
            control.check()

    def retry(fn, what: str, base_done: int = 0):
        for attempt in range(4):
            check()
            cb = control.progress_callback(base_done) if control is not None else None
            try:
                return fn(bucket(force=attempt > 0), cb)
            except Cancelled:
                raise
            except oss2.exceptions.OssError as e:
                if attempt == 3:
                    raise CloudError(f"OSS {what} 失敗：{e}") from e
                log.warning("OSS %s 失敗（第 %d 次），重試：%s", what, attempt + 1, e)
            except OSError as e:  # 網路中斷、NAS 讀取錯誤
                if attempt == 3:
                    raise CloudError(f"{what} 失敗：{e}") from e
                log.warning("%s 失敗（第 %d 次），重試：%s", what, attempt + 1, e)
            time.sleep(2 ** attempt)

    headers = {"x-oss-callback": _b64(callback)}
    if callback_var:
        headers["x-oss-callback-var"] = _b64(callback_var)

    with open(long_path(local_path), "rb") as f:
        if size <= part_size:
            data = f.read()
            result = retry(lambda b, cb: b.put_object(object_key, data, headers=headers, progress_callback=cb),
                           "上傳")
        else:
            done_parts: dict[int, object] = {}
            if upload_id:
                try:
                    for p in oss2.PartIterator(bucket(), object_key, upload_id):
                        done_parts[p.part_number] = p
                    log.info("續傳 %s：已完成 %d 個分片", local_path, len(done_parts))
                except oss2.exceptions.OssError as e:  # NoSuchUpload 等：重新開始
                    log.info("無法續傳（%s），重新上傳 %s", getattr(e, "code", e), local_path)
                    upload_id, done_parts = None, {}
            if not upload_id:
                upload_id = retry(lambda b, cb: b.init_multipart_upload(object_key).upload_id, "建立分片上傳")
                if on_upload_id:
                    on_upload_id(upload_id)
            n_parts = (size + part_size - 1) // part_size
            parts = []
            for n in range(1, n_parts + 1):
                check()
                offset = (n - 1) * part_size
                length = min(part_size, size - offset)
                prev = done_parts.get(n)
                if prev is not None and getattr(prev, "size", None) == length:
                    parts.append(oss2.models.PartInfo(n, prev.etag, size=length))
                    if control is not None:
                        control.set_done(offset + length)
                    continue
                f.seek(offset)
                chunk = f.read(length)
                res = retry(lambda b, cb, n=n, c=chunk: b.upload_part(object_key, upload_id, n, c,
                                                                      progress_callback=cb),
                            f"上傳分片 {n}/{n_parts}", base_done=offset)
                parts.append(oss2.models.PartInfo(n, res.etag, size=length, part_crc=res.crc))
            result = retry(lambda b, cb: b.complete_multipart_upload(object_key, upload_id, parts, headers=headers),
                           "完成分片上傳", base_done=size)
    try:
        body = result.resp.read()
        return json.loads(body) if body else {}
    except (ValueError, AttributeError):
        return {}
