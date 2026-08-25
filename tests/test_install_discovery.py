from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from hearthstone_companion_under_test import install_discovery


def _make_install(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "Hearthstone.exe").write_bytes(b"")
    (root / "Logs").mkdir()
    return root


@pytest.mark.skipif(os.name != "nt", reason="Windows install discovery")
def test_battle_net_config_finds_valid_default_library(tmp_path: Path) -> None:
    install = _make_install(tmp_path / "library" / "Hearthstone")
    config = tmp_path / "Battle.net.config"
    config.write_text(
        json.dumps(
            {"Client": {"Install": {"DefaultInstallPath": str(install.parent)}}}
        ),
        encoding="utf-8",
    )
    assert install_discovery._battle_net_default_install_path(config) == install.resolve()


def test_battle_net_config_rejects_invalid_untrusted_paths(tmp_path: Path) -> None:
    config = tmp_path / "Battle.net.config"
    for value in ("relative/path", r"\\server\share", r"\\?\C:\device", "https://example.test"):
        config.write_text(
            json.dumps({"Client": {"Install": {"DefaultInstallPath": value}}}),
            encoding="utf-8",
        )
        assert install_discovery._battle_net_default_install_path(config) is None


def test_battle_net_config_is_bounded_and_structured(tmp_path: Path) -> None:
    config = tmp_path / "Battle.net.config"
    config.write_bytes(b"x" * (install_discovery._BATTLE_NET_CONFIG_MAX_BYTES + 1))
    assert install_discovery._battle_net_default_install_path(config) is None

    config.write_text("{not-json", encoding="utf-8")
    assert install_discovery._battle_net_default_install_path(config) is None

    config.write_text(json.dumps({"Client": []}), encoding="utf-8")
    assert install_discovery._battle_net_default_install_path(config) is None


@pytest.mark.skipif(os.name != "nt", reason="Windows install discovery")
def test_combined_discovery_prefers_registry_and_deduplicates(
    tmp_path: Path, monkeypatch
) -> None:
    install = _make_install(tmp_path / "Hearthstone")
    monkeypatch.setattr(
        install_discovery,
        "_registry_install_values",
        lambda: (str(install), str(install)),
    )
    monkeypatch.setattr(
        install_discovery,
        "_battle_net_default_install_path",
        lambda: install.resolve(),
    )

    assert install_discovery.hearthstone_install_paths() == (install.resolve(),)


@pytest.mark.skipif(os.name == "nt", reason="Non-Windows behavior")
def test_install_discovery_is_empty_off_windows() -> None:
    assert install_discovery.hearthstone_install_paths() == ()
