import pytest

from photofinder import cli


@pytest.fixture(autouse=True)
def stages_in_process():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli, "ISOLATE_STAGES", False)
        yield
