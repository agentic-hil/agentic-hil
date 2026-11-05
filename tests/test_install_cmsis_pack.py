"""The pinned pack reaches a fake store only after its bytes are verified."""

from __future__ import annotations

import hashlib
import http.client
import io
import sys
import urllib.error
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.bench import install_cmsis_pack as installer


@pytest.fixture
def archive(monkeypatch: pytest.MonkeyPatch) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as pack:
        pack.writestr("Keil.STM32F4xx_DFP.pdsc", "<package/>")
        pack.writestr("device.bin", b"synthetic pack payload")
    content = buffer.getvalue()
    monkeypatch.setattr(installer, "PACK_SHA256", hashlib.sha256(content).hexdigest())
    monkeypatch.setattr(installer, "PACK_SIZE", len(content))
    return content


def _raises(error: Exception) -> None:
    raise error


def test_wrong_hash_exits_before_the_cache_is_opened(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], archive: bytes,
) -> None:
    sleeps = []
    monkeypatch.setattr(installer.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(archive))
    monkeypatch.setattr(installer.time, "sleep", sleeps.append)
    monkeypatch.setattr(installer, "PACK_SHA256", "0" * 64)

    def forbidden(*args: object) -> None:
        pytest.fail("A refused hash must not open or write the store.")

    monkeypatch.setitem(sys.modules, "cmsis_pack_manager", SimpleNamespace(Cache=forbidden))
    assert installer.main() == 1
    assert sleeps == [10, 10]
    assert "does not match the reviewed SHA-256" in capsys.readouterr().err


@pytest.mark.parametrize(
    "first_attempt",
    [lambda content: io.BytesIO(content[:10]), lambda content: _raises(http.client.IncompleteRead(b""))],
    ids=["short body", "incomplete read"],
)
def test_a_download_cut_short_is_a_failed_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, archive: bytes, first_attempt: object,
) -> None:
    attempts = iter([first_attempt, io.BytesIO])
    sleeps = []
    monkeypatch.setattr(installer.urllib.request, "urlopen", lambda *a, **k: next(attempts)(archive))
    monkeypatch.setattr(installer.time, "sleep", sleeps.append)
    destination = tmp_path / "download.pack"
    installer.download(destination)
    assert destination.read_bytes() == archive
    assert sleeps == [10]


@pytest.mark.parametrize("failures", [1, 2, 3])
def test_download_retries_ten_seconds_apart_and_third_failure_is_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, archive: bytes, failures: int,
) -> None:
    calls = []
    sleeps = []

    def urlopen(url: str, *, timeout: int) -> io.BytesIO:
        calls.append((url, timeout))
        if len(calls) <= failures:
            raise urllib.error.URLError("scripted download failure")
        return io.BytesIO(archive)

    monkeypatch.setattr(installer.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(installer.time, "sleep", sleeps.append)
    destination = tmp_path / "download.pack"
    if failures == 3:
        with pytest.raises(urllib.error.URLError):
            installer.download(destination)
    else:
        installer.download(destination)
        assert destination.read_bytes() == archive
    assert calls == [(installer.PACK_URL, installer.DOWNLOAD_TIMEOUT_S)] * min(failures + 1, 3)
    assert sleeps == [10] * min(failures, 2)


def test_verified_archive_is_registered_by_pdsc_and_copied_to_the_caches_pack_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, archive: bytes,
) -> None:
    store = tmp_path / "store"
    expected = "Keil/STM32F4xx_DFP/3.1.1.pack"
    calls = []

    class Cache:
        data_path = str(store)
        index = {"part": "scripted device"}

        def __init__(self, *args: object) -> None:
            assert args == (True, True)

        def add_pack_from_path(self, path: str) -> None:
            pdsc = Path(path)
            assert pdsc.name == "Keil.STM32F4xx_DFP.pdsc"
            assert pdsc.read_text() == "<package/>"
            assert store.is_dir(), "the real cache cannot write its index into a store that is not there"
            calls.append("pdsc")

        def packs_for_devices(self, devices: object) -> list[SimpleNamespace]:
            assert list(devices) == ["scripted device"]
            assert calls == ["pdsc"]
            return [SimpleNamespace(get_pack_name=lambda: expected)]

    monkeypatch.setitem(sys.modules, "cmsis_pack_manager", SimpleNamespace(Cache=Cache))
    monkeypatch.setattr(installer.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(archive))
    assert installer.main() == 0
    assert (store / expected).read_bytes() == archive
    assert [p.relative_to(store).as_posix() for p in store.rglob("*") if p.is_file()] == [expected]


def test_the_download_pin_matches_the_reviewed_release() -> None:
    assert installer.PACK_VERSION == "3.1.1"
    assert installer.PACK_SIZE == 2085496
    assert installer.PACK_SHA256 == "345231106fe697df24bbe9133aeeec1b0383d7891c4adf4f47bbb8b478e9f2f0"
