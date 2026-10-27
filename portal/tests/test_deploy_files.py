"""The container and the update service must be able to start: Linux scripts keep Unix line endings, the Containerfile strips any CR from the entrypoint, and the updater does not wait the
full time for a portal that keeps crashing. (Release 20261027-0850 died at start with "exec /usr/local/bin/itam-entrypoint: no such file or directory": '#!/bin/sh' + a carriage return.)"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
CR = b"\r"


def _linux_files():
    files = set(DEPLOY.rglob("*.sh")) | {DEPLOY / "entrypoint.sh"}
    for sub in ("updater", "quadlet"):
        files |= {p for p in (DEPLOY / sub).rglob("*") if p.is_file() and "__pycache__" not in p.parts}
    return sorted(files)


def test_linux_scripts_and_unit_files_have_unix_line_endings():
    files = _linux_files()
    assert len(files) > 10
    assert [str(p.relative_to(ROOT)) for p in files if CR in p.read_bytes()] == []


def test_the_containerfile_strips_any_cr_from_the_entrypoint():
    text = (DEPLOY / "Containerfile").read_text(encoding="utf-8")
    assert "sed -i 's/" + chr(92) + "r$//' /usr/local/bin/itam-entrypoint" in text
    assert text.index("sed -i") < text.index("chmod 0755 /usr/local/bin/itam-entrypoint")


def test_git_keeps_linux_files_unix_whatever_the_windows_setting():
    rules = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.sh text eol=lf" in rules and "deploy/updater/** text eol=lf" in rules and "deploy/entrypoint.sh text eol=lf" in rules


def test_the_updater_gives_up_early_on_a_crash_looping_portal():
    text = (DEPLOY / "updater" / "itam-updater.sh").read_text(encoding="utf-8")
    assert "NRestarts" in text and "CRASH_LIMIT" in text and "crashed $CRASH_LIMIT times" in text


def test_the_release_build_refuses_linux_files_with_windows_line_endings():
    text = (DEPLOY / "build-release.ps1").read_bytes().decode("utf-8", "replace")
    assert "-contains 13" in text and "Not building: convert them to Unix line endings" in text
