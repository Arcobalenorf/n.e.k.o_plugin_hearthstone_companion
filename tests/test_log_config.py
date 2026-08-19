from __future__ import annotations

import configparser
from pathlib import Path

import pytest
from hearthstone_companion_under_test import log_config


def read_config(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(path, encoding="utf-8")
    return parser


def test_log_config_preserves_sections_backs_up_and_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "log.config"
    original = "[Custom]\nKeepMe=yes\n\n[Power]\nLogLevel=0\nFilePrinting=false\n"
    target.write_text(original, encoding="utf-8")

    first = log_config.ensure_power_log_config(target)
    backup = Path(first["backup_path"])
    configured = read_config(target)

    assert first["changed"] is True
    assert first["restart_required"] is True
    assert backup.read_text(encoding="utf-8") == original
    assert configured.get("Custom", "KeepMe") == "yes"
    assert configured.get("Power", "LogLevel") == "1"
    assert configured.getboolean("Power", "FilePrinting") is True
    assert configured.getboolean("Power", "Verbose") is True
    assert configured.getboolean("Power", "ConsolePrinting") is False
    assert configured.getboolean("Power", "ScreenPrinting") is False
    assert configured.getboolean("LoadingScreen", "FilePrinting") is True

    content_after_first_write = target.read_bytes()
    second = log_config.ensure_power_log_config(target)
    assert second["changed"] is False
    assert second["restart_required"] is False
    assert target.read_bytes() == content_after_first_write
    assert backup.read_text(encoding="utf-8") == original


def test_new_log_config_has_no_fake_backup(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "log.config"

    result = log_config.ensure_power_log_config(target)

    assert result["changed"] is True
    assert result["backup_path"] == ""
    assert not target.with_suffix(".config.neko.bak").exists()


def test_atomic_replace_failure_keeps_original_and_cleans_temp(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "log.config"
    original = b"[Power]\nLogLevel=0\n"
    target.write_bytes(original)

    def fail_replace(source: str, destination: Path) -> None:
        assert Path(source).parent == target.parent
        assert Path(destination) == target.resolve()
        raise OSError("simulated replace failure")

    monkeypatch.setattr(log_config.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        log_config.ensure_power_log_config(target)

    assert target.read_bytes() == original
    assert list(tmp_path.glob("log.config.*.tmp")) == []
    assert target.with_suffix(".config.neko.bak").read_bytes() == original


def test_log_config_uses_atomic_replace_in_target_directory(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "log.config"
    calls: list[tuple[Path, Path]] = []
    real_replace = log_config.os.replace

    def record_replace(source: str, destination: Path) -> None:
        calls.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(log_config.os, "replace", record_replace)
    log_config.ensure_power_log_config(target)

    assert len(calls) == 1
    source, destination = calls[0]
    assert source.parent == target.parent
    assert destination == target.resolve()
    assert not source.exists()


@pytest.mark.parametrize(
    "content",
    [
        "[Power]\nLogLevel=1\nloglevel=0\n",
        "[Power]\nLogLevel=1\n[Power]\nVerbose=true\n",
        "[power]\nLogLevel=1\n",
        "[Power\nLogLevel=1\n",
    ],
)
def test_ambiguous_or_malformed_managed_config_is_never_overwritten(tmp_path: Path, content: str) -> None:
    target = tmp_path / "log.config"
    target.write_text(content, encoding="utf-8")

    with pytest.raises(log_config.LogConfigError):
        log_config.ensure_power_log_config(target)

    assert target.read_text(encoding="utf-8") == content
    assert not target.with_suffix(".config.neko.bak").exists()
    assert list(tmp_path.glob("log.config.*.tmp")) == []
