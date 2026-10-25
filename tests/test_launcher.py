"""Check that the installed package and repository launcher are usable."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("entry_point", ["repository", "module", "console"])
def test_help_works_outside_the_repository(entry_point: str, tmp_path: Path) -> None:
    commands = {
        "repository": [str(REPOSITORY / "run_agent")],
        "module": [sys.executable, "-m", "prior_auth_agent"],
        "console": [str(Path(sys.executable).with_name("prior-auth-agent"))],
    }
    completed = subprocess.run(
        [*commands[entry_point], "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    for option in ("--portal-url", "--patient", "--credentials", "--out"):
        assert option in completed.stdout


def test_launcher_explains_missing_environment(tmp_path: Path) -> None:
    directory = tmp_path / "checkout with spaces"
    directory.mkdir()
    launcher = directory / "run_agent"
    shutil.copy2(REPOSITORY / "run_agent", launcher)

    completed = subprocess.run(
        [str(launcher), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 2
    assert "uv sync --locked" in completed.stderr
    assert completed.stdout == ""
