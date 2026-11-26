import pytest

from fakes import World


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path)
    monkeypatch.setenv("HOME", str(w.home))
    return w
