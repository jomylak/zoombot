from unittest import mock
from bot import config, notify

def test_limits(tmp_path):
    notify._RATE_FILE = tmp_path / "r.json"
    config.NOTIFY_PAUSED = False
    assert notify._allow("a", "1")
    assert not notify._allow("a", "1")           # duplicate dropped
    assert all(notify._allow("b", str(i)) for i in range(5))
    assert not notify._allow("c", "x")           # window cap (6) hit
    config.NOTIFY_PAUSED = True
    assert not notify._allow("z", "z")

if __name__ == "__main__":
    import pathlib, tempfile
    test_limits(pathlib.Path(tempfile.mkdtemp())); print("ok")
