import pytest

from repka.config import state_dir
from repka.runlock import LockBusy, RunLock, run_exclusive


def test_second_lock_raises_lock_busy(tmp_path):
    path = tmp_path / "run.lock"
    with RunLock(path):
        with pytest.raises(LockBusy):
            with RunLock(path):
                pass


def test_run_exclusive_busy_is_a_friendly_exit_zero(home, capsys):
    with RunLock(state_dir() / "run.lock"):
        assert run_exclusive(lambda: 5) == 0
        assert run_exclusive(lambda: 5, enabled=False) == 5  # read-only modes skip the lock
    assert "another repka run is in progress" in capsys.readouterr().err
    assert run_exclusive(lambda: 5) == 5
