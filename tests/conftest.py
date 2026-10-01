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
