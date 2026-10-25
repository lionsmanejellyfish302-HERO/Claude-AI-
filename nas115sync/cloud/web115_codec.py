"""115 電腦版／網頁版通訊協定的編碼：與 115 官方客戶端互通所需，本程式自行實作。

- 上傳初始化（秒傳）的請求以固定金鑰 AES-128-CBC 加密，回應為 AES 加密後的 LZ4 區塊
- 上傳簽章：sig（SHA-1）與 token（MD5），以及帶時間戳的 k_ec 參數
- 取得下載網址的請求／回應使用 115 的「m115」編碼（XOR 金鑰表 + 115 公開的 RSA 公鑰）

這些金鑰、鹽值、XOR 表都是 115 協定公開固定的常數（每個 115 客戶端都相同），不是使用者的秘密。
協定細節參考自開源專案 p115client（MIT 授權，作者 ChenyangGao）的文件。
"""
from __future__ import annotations

import base64
import hashlib
import json
import zlib
from urllib.parse import urlencode

APP_VERSION = "36.2.28"

_AES_KEY = bytes.fromhex("fb1a19d652f5aaf7bc651d0f69bf422f")
_AES_IV = bytes.fromhex("69bf422f49960550a0ad44ec3446cb4c")
_EC_PUBKEY = bytes.fromhex("1d030e80a178dceececda377de128d8ed9ddcf55ae61ed46ea121a1cfc81")
_CRC_SALT = b"^j>WD3Kr?J2gLFjD4W2y@"
_MD5_SALT = b"Qclm8MGWUv59TnrR0XPg"

_KTS = bytes.fromhex(
    "f0e569aebfdcbf8a1a45e8be7da673b8de8fe7c445da86c49b648b146ab4f1aa"
    "3801359e26692c86006b4fa5363462a62a966818f24afdbd6b978f4d8f8913b7"
    "6c8e93ed0e0d483ed72f88d8fefe7e8650954fd1eb832634db667b9c7e9d7a81"
    "32eab633de3aa95934663baaba816048b9d5819cf86c8477ff5478265fbee81e"
    "369f34805c452c9b76d51b8fccc3b8f5"
)
_RSA_N = int(
    "8686980c0f5a24c4b9d43020cd2c22703ff3f450756529058b1cf88f09b86021"
    "36477198a6e2683149659bd122c33592fdb5ad47944ad1ea4d36c6b172aad633"
    "8c3bb6ac6227502d010993ac967d1aef00f0c8e038de2e4d3bc2ec368af2e9f1"
    "0a6f1eda4f7262f136420c07c331b871bf139f74f3010e3c4fe57df3afb71683", 16)
_RSA_E = 0x10001
_RSA_BLOCK = 128


# --------------------------------------------------------------------------- AES + LZ4
def _cipher():
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    return Cipher(algorithms.AES(_AES_KEY), modes.CBC(_AES_IV))


def aes_encrypt(data: bytes) -> bytes:
    pad = -len(data) % 16  # 115 的補齊方式：剛好是 16 的倍數時不補
    enc = _cipher().encryptor()
    return enc.update(data + bytes([pad]) * pad) + enc.finalize()


def aes_decrypt(data: bytes) -> bytes:
    dec = _cipher().decryptor()
    out = dec.update(data[: len(data) // 16 * 16]) + dec.finalize()
    pad = out[-1] if out else 0
    if 0 < pad < 16 and out.endswith(bytes([pad]) * pad):
        out = out[:-pad]
    return out


def lz4_block_decompress(src: bytes) -> bytes:
    """標準 LZ4 區塊格式解壓縮。"""
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while i < n:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        out += src[i:i + lit]
        i += lit
        if i >= n:  # 最後一段只有字面值
            break
        if i + 2 > n:
            raise ValueError("LZ4 資料不完整")
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        if offset == 0 or offset > len(out):
            raise ValueError("LZ4 位移錯誤")
        mlen = token & 15
        if mlen == 15:
            while i < n:
                b = src[i]
                i += 1
                mlen += b
                if b != 255:
                    break
        mlen += 4
        start = len(out) - offset
        for k in range(mlen):  # 允許重疊複製
            out.append(out[start + k])
    return bytes(out)


def lz4_unpack(data: bytes) -> bytes:
    """115 的格式：連續的「2 位元組小端長度 + LZ4 區塊」。"""
    out = bytearray()
    i = 0
    while i + 2 <= len(data):
        size = data[i] | (data[i + 1] << 8)
        if size == 0:
            break
        out += lz4_block_decompress(data[i + 2:i + 2 + size])
        i += 2 + size
    return bytes(out)


def decrypt_response(content: bytes) -> bytes:
    return lz4_unpack(aes_decrypt(content))


def maybe_decrypt(content: bytes) -> bytes:
    """有些 115 介面回傳明文 JSON，有些回傳加密內容。"""
    s = content.strip()
    if s[:1] + s[-1:] in (b"{}", b"[]", b'""'):
        return content
    return decrypt_response(content)


# --------------------------------------------------------------------------- 上傳簽章
def _sha1(b: bytes) -> str:  # 115 協定規定的簽章算法，不是本程式用來保護資料的雜湊
    return hashlib.sha1(b, usedforsecurity=False).hexdigest()


def _md5(b: bytes) -> str:
    return hashlib.md5(b, usedforsecurity=False).hexdigest()


def ec_token(ts: int) -> str:
    tok = bytearray(_EC_PUBKEY[:15])
    tok += b"\x00s\x00\x00\x00"
    tok += ts.to_bytes(4, "little")
    tok += _EC_PUBKEY[15:]
    tok += b"\x00\x01\x00\x00\x00"
    tok += (zlib.crc32(_CRC_SALT + bytes(tok)) & 0xFFFFFFFF).to_bytes(4, "little")
    return base64.b64encode(bytes(tok)).decode("ascii")


def sign_upload(fields: dict, userkey: str, ts: int) -> tuple[dict, bytes]:
    """回傳 (網址參數, 加密後的表單內容)。fields 需含 userid、fileid、target、filesize、appversion。"""
    p = dict(fields)
    p.setdefault("sign_key", "")
    p.setdefault("sign_val", "")
    p["userkey"] = userkey
    p["t"] = ts
    inner = _sha1(f"{p['userid']}{p['fileid']}{p['target']}0".encode("ascii"))
    p["sig"] = _sha1(userkey.encode("ascii") + inner.encode("ascii") + b"000000").upper()
    uid_md5 = _md5(str(int(p["userid"])).encode("ascii"))
    raw = (f"{p['fileid']}{p['filesize']}{p['sign_key']}{p['sign_val']}{p['userid']}{ts}{uid_md5}"
           f"{p['appversion']}")
    p["token"] = _md5(_MD5_SALT + raw.encode("ascii"))
    body = urlencode(sorted((k, v) for k, v in p.items() if v)).encode("latin-1")
    return {"k_ec": ec_token(ts)}, aes_encrypt(body)


# --------------------------------------------------------------------------- m115（取得下載網址）
def _xor_key(seed: bytes, n: int) -> bytes:
    out = bytearray(n)
    hi, lo = n * (n - 1), 0
    for i in range(n):
        out[i] = _KTS[hi] ^ ((seed[i] + _KTS[lo]) & 0xFF)
        hi -= n
        lo += n
    return bytes(out)


def _xor(data: bytes, key: bytes) -> bytes:
    """前 len%4 個位元組用金鑰開頭，其餘從金鑰開頭循環。"""
    head = len(data) % 4
    out = bytearray(data)
    for i in range(head):
        out[i] ^= key[i]
    for j in range(head, len(out)):
        out[j] ^= key[(j - head) % len(key)]
    return bytes(out)


_SEED = bytes(16)
_KEY4 = _xor_key(_SEED, 4)
_KEY_L = bytes.fromhex("7806ad4c33865d184c013f46")  # 請求固定使用的 12 位元組 XOR 金鑰


def m115_encode(data: bytes) -> str:
    body = _SEED + _xor(_xor(data, _KEY4)[::-1], _KEY_L)
    out = bytearray()
    chunk = _RSA_BLOCK - 11
    for i in range(0, len(body), chunk):
        part = body[i:i + chunk]
        padded = b"\x00" + b"\x02" * (_RSA_BLOCK - 2 - len(part)) + b"\x00" + part
        out += pow(int.from_bytes(padded, "big"), _RSA_E, _RSA_N).to_bytes(_RSA_BLOCK, "big")
    return base64.b64encode(bytes(out)).decode("ascii")


def m115_decode(text: str | bytes) -> bytes:
    raw = base64.b64decode(text)
    plain = bytearray()
    for i in range(0, len(raw), _RSA_BLOCK):
        m = pow(int.from_bytes(raw[i:i + _RSA_BLOCK], "big"), _RSA_E, _RSA_N)
        b = m.to_bytes((m.bit_length() + 7) // 8, "big")
        plain += b[b.index(0) + 1:]
    seed, rest = bytes(plain[:16]), bytes(plain[16:])
    return _xor(_xor(rest, _xor_key(seed, 12))[::-1], _KEY4)


def m115_encode_json(obj) -> str:
    return m115_encode(json.dumps(obj, separators=(",", ":")).encode("utf-8"))
