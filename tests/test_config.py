import importlib
import os
from pathlib import Path

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


def test_default_data_root_is_the_checkouts_data_dir():
    assert config.DEFAULT_DATA_ROOT == Path(__file__).resolve().parents[1] / "data"


def test_env_overrides_data_root_and_models_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("PHOTOFINDER_DATA_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("PHOTOFINDER_MODELS_DIR", str(tmp_path / "weights"))
    try:
        importlib.reload(config)
        assert config.DATA_ROOT == tmp_path / "root"
        assert config.MODELS_DIR == tmp_path / "weights"
    finally:
        monkeypatch.undo()
        importlib.reload(config)
    assert config.MODELS_DIR == config.DEFAULT_DATA_ROOT / "models"


def test_require_mounted_creates_the_default_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_DATA_ROOT", tmp_path / "data")
    config.require_mounted(tmp_path / "data")
    assert (tmp_path / "data").is_dir()


def test_require_mounted_refuses_a_missing_custom_root(tmp_path):
    with pytest.raises(SystemExit, match="does not exist"):
        config.require_mounted(tmp_path / "unmounted" / "data")
    assert not (tmp_path / "unmounted").exists()
