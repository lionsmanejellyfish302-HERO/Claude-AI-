"""115 非秒傳檔案的實際上傳：依 upload/init 回傳的 bucket/object 與 STS 憑證上傳到阿里雲 OSS，
並帶上 115 的 callback，讓 115 在上傳完成後建立檔案。"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
from typing import Callable

from ..paths import long_path
from .base import CloudError, ProgressFn

log = logging.getLogger(__name__)

TOKEN_MAX_AGE = 40 * 60  # STS 憑證約 1 小時有效，提早更新


class UploadCancelled(CloudError):
    pass


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
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
) -> dict:
    """上傳並回傳 115 callback 的 JSON（可能為空 dict）。"""
    import oss2  # 延遲載入：測試與秒傳不需要

    state = {"token_at": 0.0, "bucket": None}

    def bucket(force: bool = False):
        if force or state["bucket"] is None or time.time() - state["token_at"] > TOKEN_MAX_AGE:
            tok = token_provider()
            auth = oss2.StsAuth(tok["AccessKeyId"], tok["AccessKeySecret"], tok["SecurityToken"])
            state["bucket"] = oss2.Bucket(auth, _endpoint(tok["endpoint"]), bucket_name)
            state["token_at"] = time.time()
        return state["bucket"]

    def check_cancel():
        if cancel is not None and cancel.is_set():
            raise UploadCancelled("使用者取消上傳")

    def retry(fn, what: str):
        for attempt in range(4):
            check_cancel()
            try:
                return fn(bucket(force=attempt > 0))
            except oss2.exceptions.OssError as e:
                if attempt == 3:
                    raise CloudError(f"OSS {what} 失敗：{e}") from e
                log.warning("OSS %s 失敗（第 %d 次），重試：%s", what, attempt + 1, e)
                time.sleep(2 ** attempt)
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

            def put(b):
                cb = (lambda done, total: progress(done, size)) if progress else None
                return b.put_object(object_key, data, headers=headers, progress_callback=cb)

            result = retry(put, "上傳")
        else:
            upload_id = retry(lambda b: b.init_multipart_upload(object_key).upload_id, "建立分片上傳")
            parts = []
            done = 0
            try:
                part_no = 1
                while True:
                    check_cancel()
                    chunk = f.read(part_size)
                    if not chunk:
                        break
                    res = retry(lambda b, n=part_no, c=chunk: b.upload_part(object_key, upload_id, n, c),
                                f"上傳分片 {part_no}")
                    parts.append(oss2.models.PartInfo(part_no, res.etag, size=len(chunk), part_crc=res.crc))
                    done += len(chunk)
                    if progress:
                        progress(done, size)
                    part_no += 1
                result = retry(lambda b: b.complete_multipart_upload(object_key, upload_id, parts, headers=headers),
                               "完成分片上傳")
            except BaseException:
                try:
                    bucket().abort_multipart_upload(object_key, upload_id)
                except Exception:  # noqa: BLE001 - 中止失敗不影響原錯誤
                    pass
                raise

    try:
        body = result.resp.read()
        return json.loads(body) if body else {}
    except (ValueError, AttributeError):
        return {}
