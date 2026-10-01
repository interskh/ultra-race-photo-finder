import json

import pytest

from photofinder.sources import pailixiang, photoplus
from photofinder.sources.common import Blocked, MissingSiteKey, site_key


def test_env_wins_over_the_keys_file(tmp_path, monkeypatch):
    keys = tmp_path / "site-keys.json"
    keys.write_text(json.dumps({"photoplus_salt": "from-file"}))
    monkeypatch.setenv("PHOTOFINDER_SITE_KEYS", str(keys))
    assert site_key("photoplus_salt") == "test-salt"
    monkeypatch.delenv("PHOTOFINDER_PHOTOPLUS_SALT")
    assert site_key("photoplus_salt") == "from-file"


@pytest.mark.parametrize("content", [None, "{}", "not json", "[]"])
def test_missing_key_names_the_env_var_and_the_file(tmp_path, monkeypatch, content):
    keys = tmp_path / "site-keys.json"
    if content is not None:
        keys.write_text(content)
    monkeypatch.setenv("PHOTOFINDER_SITE_KEYS", str(keys))
    monkeypatch.delenv("PHOTOFINDER_PAILIXIANG_KEY")
    with pytest.raises(MissingSiteKey, match=f"PHOTOFINDER_PAILIXIANG_KEY or add \"pailixiang_key\" to {keys}"):
        site_key("pailixiang_key")
    assert issubclass(MissingSiteKey, Blocked)


def test_both_adapters_need_their_key(monkeypatch):
    monkeypatch.delenv("PHOTOFINDER_PAILIXIANG_KEY")
    monkeypatch.delenv("PHOTOFINDER_PHOTOPLUS_SALT")
    with pytest.raises(MissingSiteKey):
        pailixiang.ak()
    with pytest.raises(MissingSiteKey):
        photoplus.sign({"activityNo": 1}, 0)
