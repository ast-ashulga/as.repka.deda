import pytest

from repka.runlock import LockBusy, RunLock


def test_second_lock_raises_lock_busy(tmp_path):
    path = tmp_path / "run.lock"
    with RunLock(path):
        with pytest.raises(LockBusy):
            with RunLock(path):
                pass
