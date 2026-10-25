import hashlib

from nas115sync.config import Config
from nas115sync.hashing import PREID_SIZE, hash_file, sha1_range
from nas115sync.paths import IgnoreMatcher, cloud_norm, norm_key
from nas115sync.savedata import SavedataDetector


def test_hash_file_md5_sha1_preid(tmp_path):
    data = b"x" * (PREID_SIZE + 12345)
    p = tmp_path / "f.bin"
    p.write_bytes(data)
    h = hash_file(str(p))
    assert h.size == len(data)
    assert h.md5 == hashlib.md5(data).hexdigest()
    assert h.sha1 == hashlib.sha1(data).hexdigest().upper()
    assert h.preid == hashlib.sha1(data[:PREID_SIZE]).hexdigest().upper()


def test_small_file_preid_is_whole_file(tmp_path):
    p = tmp_path / "s.bin"
    p.write_bytes(b"abc")
    h = hash_file(str(p))
    assert h.preid == h.sha1


def test_sha1_range_inclusive(tmp_path):
    p = tmp_path / "r.bin"
    p.write_bytes(bytes(range(100)))
    assert sha1_range(str(p), 10, 19) == hashlib.sha1(bytes(range(10, 20))).hexdigest().upper()


def test_norm_key_case_and_unicode():
    assert norm_key("Photos\\Café.JPG") == norm_key("photos/Café.jpg")
    assert cloud_norm("備份\\NAS//Photos/") == "/備份/NAS/Photos"


def test_ignore_matcher():
    m = IgnoreMatcher(["Thumbs.db", "~$*", "@eaDir"])
    assert m.match_name("thumbs.DB")
    assert m.match_rel("a/@eaDir/b.jpg")
    assert m.match_name("~$報告.docx")
    assert not m.match_rel("a/b.jpg")


def test_savedata_detector():
    d = SavedataDetector(["SaveData", "Saves"], [".sav"])
    assert d.group_of("Games/X/SaveData/slot1/data.bin") == "Games/X/SaveData"
    assert d.group_of("Games/Y/profile.SAV") == "Games/Y"
    assert d.group_of("Docs/readme.txt") is None
    assert d.group_of("GameA/slot.dat", whole_mapping=True) == "GameA"
    assert d.group_of("root.dat", whole_mapping=True) == ""


def test_config_roundtrip(tmp_path):
    c = Config(tmp_path / "c.json")
    m = c.add_mapping("\\\\NAS\\home\\Photos", "備份/Photos")
    c.confirm_mapping(m.id, "123")
    c.settings.savedata_keep_versions = 3
    c.save()
    c2 = Config(tmp_path / "c.json")
    assert c2.mappings[0].cloud_path == "/備份/Photos"
    assert c2.mappings[0].confirmed and c2.mappings[0].cloud_cid == "123"
    assert c2.settings.savedata_keep_versions == 3
    assert c2.active_mappings()[0].id == m.id
