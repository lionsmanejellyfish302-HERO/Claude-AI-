"""115 協定編碼：與公開參考實作（p115cipher）產生的測試向量逐位元比對。

tests/data/web115_vectors.json 由參考實作在隔離環境產生，本程式不依賴該套件。
"""
import base64
import json
from pathlib import Path

import pytest

from nas115sync.cloud import web115_codec as C

V = json.loads((Path(__file__).parent / "data" / "web115_vectors.json").read_text(encoding="utf-8"))


def b64(s):
    return base64.b64decode(s)


def test_xor_keys_match_protocol_constants():
    assert C._KEY4.hex() == V["keys"]["k4"] == V["keys"]["RSA_KEY"]
    assert C._KEY_L.hex() == V["keys"]["G_key_l"]
    assert C._xor_key(bytes(16), 12).hex() == V["keys"]["k12"]


@pytest.mark.parametrize("ts", list(V["ec_token"]))
def test_ec_token(ts):
    assert C.ec_token(int(ts)) == V["ec_token"][ts]


@pytest.mark.parametrize("case", V["upload"], ids=lambda c: c["in"]["filename"])
def test_upload_signature_and_body(case):
    fields = {k: v for k, v in case["in"].items() if k != "userkey"}
    params, body = C.sign_upload(fields, case["in"]["userkey"], case["t"])
    assert params == case["params"]
    assert body == b64(case["data"])


@pytest.mark.parametrize("plain,cipher", V["rsa_enc"], ids=lambda x: str(len(x)))
def test_m115_encode(plain, cipher):
    assert C.m115_encode(b64(plain)) == cipher


@pytest.mark.parametrize("cipher,plain", V["rsa_dec"], ids=lambda x: str(len(x)))
def test_m115_decode(cipher, plain):
    assert C.m115_decode(cipher) == b64(plain)


@pytest.mark.parametrize("plain,cipher", V["aes"], ids=lambda x: str(len(x)))
def test_aes(plain, cipher):
    assert C.aes_encrypt(b64(plain)) == b64(cipher)
    assert C.aes_decrypt(b64(cipher)) == b64(plain)


@pytest.mark.parametrize("cipher,plain", V["resp"], ids=["small", "multi_block"])
def test_encrypted_lz4_response(cipher, plain):
    assert C.decrypt_response(b64(cipher)) == b64(plain)
    assert C.maybe_decrypt(b64(cipher)) == b64(plain)
    assert json.loads(C.maybe_decrypt(b64(plain)))  # 明文 JSON 原樣通過


def test_lz4_rejects_corrupt_data():
    with pytest.raises(ValueError):
        C.lz4_block_decompress(b"\x1fA\x09\x00")  # 位移超出已解出的長度
