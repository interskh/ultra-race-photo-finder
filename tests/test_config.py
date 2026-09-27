import os

import pytest

from photofinder import config

ENV = ("HF_HOME", "HF_HUB_CACHE", "TORCH_HOME", "YOLO_CONFIG_DIR", "XDG_CACHE_HOME", "MPLCONFIGDIR", "HF_HUB_OFFLINE")


@pytest.fixture
def clean_env(monkeypatch):
    for key in ENV:
        monkeypatch.delenv(key, raising=False)


def snapshot(models_dir, *files):
    snap = models_dir / "huggingface" / "hub" / config.SIGLIP_REPO / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    for f in files:
        (snap / f).write_bytes(b"x")


def test_hf_offline_set_when_siglip_and_tokenizer_cached(tmp_path, clean_env):
    snapshot(tmp_path, *config.SIGLIP_FILES)
    config.setup_model_env(tmp_path)
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["HF_HUB_CACHE"] == str(tmp_path / "huggingface" / "hub")


@pytest.mark.parametrize("files", [(), ("open_clip_model.safetensors",)])
def test_hf_offline_unset_until_siglip_fully_cached(tmp_path, clean_env, files):
    if files:
        snapshot(tmp_path, *files)
    config.setup_model_env(tmp_path)
    assert "HF_HUB_OFFLINE" not in os.environ
    assert os.environ["HF_HOME"] == str(tmp_path / "huggingface")
