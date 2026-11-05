"""A scripted runtime exercises lock ordering without containers or downloads."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tools import check_image_pip_locks as checker

FAKE_RUNTIME = r'''
import json
import re
import sys
from pathlib import Path

state_file = Path(sys.argv[1])
state = json.loads(state_file.read_text())
args = sys.argv[2:]
state['commands'].append(args)
status = 0
if args[0] == 'run':
    mount = args[args.index('--volume') + 1]
    assert mount.endswith(':/checkout:ro')
    state['checkout'] = mount.removesuffix(':/checkout:ro')
    print('scripted-container')
elif args[0] == 'exec':
    if '--version' in args:
        print('pip ' + state['pip'])
    elif 'install' in args:
        lock = args[args.index('-r') + 1].removeprefix('/checkout/')
        assert '--require-hashes' in args
        if lock == 'requirements/container.txt':
            assert '--dry-run' not in args and '--ignore-installed' not in args
            content = (Path(state['checkout']) / lock).read_text()
            match = re.search(r'^pip==(\S+)', content, re.M)
            if match:
                state['pip'] = match[1]
        else:
            assert '--dry-run' in args and '--ignore-installed' in args
            if state['pip'] == '23.0.1' or state.get('fail_lock') == lock:
                print('scripted resolver failure', file=sys.stderr)
                status = 1
elif args[0] == 'rm':
    state['removed'] = True
    status = int(state.get('fail_cleanup', False))
else:
    raise AssertionError(args)
state_file.write_text(json.dumps(state))
sys.exit(status)
'''


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    script = tmp_path / "runtime.py"
    script.write_text(FAKE_RUNTIME, encoding="utf-8")
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"commands": [], "pip": "23.0.1"}))
    actual_run = subprocess.run

    def run(command: list[str], **options: object) -> subprocess.CompletedProcess:
        assert command[0] in {"docker", "podman"}
        return actual_run([sys.executable, str(script), str(state_path), *command[1:]], **options)

    monkeypatch.setattr(checker.subprocess, "run", run)
    return state_path


@pytest.mark.parametrize("image", checker.IMAGES)
@pytest.mark.parametrize("engine", ["docker", "podman"])
def test_each_base_uses_its_python_then_replaces_pip_before_resolving_the_other_locks(
    runtime: Path, image: str, engine: str,
) -> None:
    assert checker.main(["--image", image, "--runtime", engine]) == 0
    state = json.loads(runtime.read_text())
    commands = state["commands"]
    base, setup = checker.image_base(image)
    assert base in commands[0]
    assert "@sha256:" in base
    assert any(command[2:] == ["python", "-m", "venv", "/work/.venv"] for command in commands)
    installs = [command for command in commands if "install" in command]
    assert [command[-1] for command in installs] == [f"/checkout/requirements/{entry}.txt" for entry in checker.LOCKS]
    setup_commands = [command for command in commands if command[2:4] == ["sh", "-c"]]
    assert [command[-1] for command in setup_commands] == ([setup] if setup else [])
    if image != "default":
        assert set(re.findall(r"\bpython[\w-]*", setup)) == (
            {"python3", "python-unversioned-command"} if image.startswith("fedora")
            else {"python3", "python3-venv", "python-is-python3"}
        )
    assert state["pip"] == "26.3"
    assert state["removed"] is True


def test_removing_pip_from_the_bootstrap_lock_exposes_the_old_resolver(
    runtime: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "checkout"
    (root / "requirements").mkdir(parents=True)
    content = (checker.ROOT / "requirements/container.txt").read_text(encoding="utf-8")
    without_pip = re.sub(r"(?m)^pip==[^\n]*\n(?:[ \t]+[^\n]*\n)*", "", content)
    assert without_pip != content
    (root / "requirements/container.txt").write_text(without_pip, encoding="utf-8")
    base, _ = checker.image_base("debian-12")
    monkeypatch.setattr(checker, "ROOT", root)
    monkeypatch.setattr(checker, "image_base", lambda image: (base, ""))

    assert checker.main(["--image", "debian-12"]) == 1
    error = capsys.readouterr().err
    assert "image=debian-12 lock=requirements/dev.txt pip=pip 23.0.1" in error
    state = json.loads(runtime.read_text())
    assert state["removed"] is True
    assert not any(command[-1].endswith("runtime.txt") for command in state["commands"])


@pytest.mark.parametrize("lock", ["dev", "runtime", "production"])
def test_a_failed_lock_names_the_image_lock_and_current_pip_and_removes_the_container(
    runtime: Path, capsys: pytest.CaptureFixture[str], lock: str,
) -> None:
    state = json.loads(runtime.read_text())
    state["fail_lock"] = f"requirements/{lock}.txt"
    runtime.write_text(json.dumps(state))
    assert checker.main([]) == 1
    assert f"image=default lock=requirements/{lock}.txt pip=pip 26.3" in capsys.readouterr().err
    assert json.loads(runtime.read_text())["removed"] is True


def test_a_leaked_container_fails_the_check(runtime: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state = json.loads(runtime.read_text())
    state["fail_cleanup"] = True
    runtime.write_text(json.dumps(state))
    assert checker.main([]) == 1
    assert "container cleanup failed" in capsys.readouterr().err
