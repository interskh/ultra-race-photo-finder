import pytest

from photofinder import cli, config


@pytest.fixture(autouse=True)
def stages_in_process():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli, "ISOLATE_STAGES", False)
        yield


@pytest.fixture(autouse=True)
def isolated_data_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("data-root")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(config, "DATA_ROOT", root)
        yield root


@pytest.fixture(autouse=True)
def fake_site_keys(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PHOTOFINDER_PAILIXIANG_KEY", "fake-pailixiang-key-for-the-test")
        mp.setenv("PHOTOFINDER_PHOTOPLUS_SALT", "test-salt")
        mp.setenv("PHOTOFINDER_SITE_KEYS", str(tmp_path_factory.mktemp("keys") / "site-keys.json"))
        yield
