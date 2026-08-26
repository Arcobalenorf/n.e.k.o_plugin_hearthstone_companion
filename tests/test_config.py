from __future__ import annotations

import json
import os
import subprocess
import tomllib
from pathlib import Path

import pytest
from hearthstone_companion_under_test.config import CompanionConfig

ROOT = Path(__file__).resolve().parents[1]
NEKO_ROOT = ROOT.parent / "N.E.K.O"
NEKO_PYTHON = NEKO_ROOT / ".venv" / "Scripts" / "python.exe"


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], {}])
def test_explicit_non_boolean_security_settings_fail_closed(value: object) -> None:
    config = CompanionConfig.from_mapping(
        {
            "llm_do_not_disturb": value,
            "llm_data_consent": value,
            "card_catalog_network_enabled": value,
        }
    )

    assert config.llm_do_not_disturb is True
    assert config.llm_data_consent is False
    assert config.card_catalog_network_enabled is False


def test_missing_boolean_settings_keep_declared_defaults() -> None:
    config = CompanionConfig.from_mapping({})

    assert config.monitor_on_start is True
    assert config.card_catalog_network_enabled is True
    assert config.overlay_enabled is True
    assert config.llm_data_consent is True
    assert config.llm_do_not_disturb is True
    assert config.initial_read_max_bytes == 64 * 1024 * 1024


def test_legacy_lifecycle_setting_is_ignored() -> None:
    disabled = CompanionConfig.from_mapping({"llm_lifecycle_enabled": False})
    enabled = CompanionConfig.from_mapping({"llm_lifecycle_enabled": True})

    assert disabled.to_dict() == enabled.to_dict()
    assert "llm_lifecycle_enabled" not in disabled.to_dict()


@pytest.mark.parametrize(
    ("legacy_enabled", "expected_do_not_disturb"),
    [(False, True), (True, False)],
)
def test_legacy_commentary_setting_migrates_to_do_not_disturb(
    legacy_enabled: bool,
    expected_do_not_disturb: bool,
) -> None:
    config = CompanionConfig.from_mapping(
        {"llm_commentary_enabled": legacy_enabled}
    )

    assert config.llm_do_not_disturb is expected_do_not_disturb
    assert "llm_commentary_enabled" not in config.to_dict()


def test_new_do_not_disturb_setting_wins_over_legacy_commentary_setting() -> None:
    config = CompanionConfig.from_mapping(
        {
            "llm_do_not_disturb": True,
            "llm_commentary_enabled": True,
        }
    )

    assert config.llm_do_not_disturb is True


@pytest.mark.parametrize(
    ("legacy_enabled", "expected_do_not_disturb"),
    [(False, True), (True, False)],
)
def test_manifest_overlay_preserves_legacy_commentary_preference_on_upgrade(
    legacy_enabled: bool,
    expected_do_not_disturb: bool,
) -> None:
    with (ROOT / "plugin.toml").open("rb") as handle:
        manifest_section = dict(tomllib.load(handle)["hearthstone_companion"])

    # N.E.K.O overlays the active profile on top of the manifest section.
    manifest_section.update({"llm_commentary_enabled": legacy_enabled})
    config = CompanionConfig.from_mapping(manifest_section)

    assert config.llm_do_not_disturb is expected_do_not_disturb


@pytest.mark.parametrize(
    ("legacy_enabled", "expected_do_not_disturb"),
    [(False, True), (True, False)],
)
def test_neko_profile_merger_preserves_legacy_commentary_preference_on_upgrade(
    tmp_path: Path,
    legacy_enabled: bool,
    expected_do_not_disturb: bool,
) -> None:
    if not NEKO_PYTHON.is_file():
        pytest.skip("local N.E.K.O runtime is required for profile merge integration")

    with (ROOT / "plugin.toml").open("rb") as handle:
        manifest = tomllib.load(handle)
    with (ROOT / "config.example.toml").open("rb") as handle:
        runtime_config = tomllib.load(handle)
    manifest_path = tmp_path / "plugin.toml"
    manifest_path.touch()
    (tmp_path / "profiles.toml").write_text(
        '[config_profiles]\nactive = "legacy"\n\n'
        '[config_profiles.files]\nlegacy = "legacy.toml"\n',
        encoding="utf-8",
    )
    (tmp_path / "legacy.toml").write_text(
        "[hearthstone_companion]\n"
        f"llm_commentary_enabled = {str(legacy_enabled).lower()}\n",
        encoding="utf-8",
    )
    script = (
        "import json,sys;"
        "from pathlib import Path;"
        "from plugin.server.infrastructure.config_merge import deep_merge;"
        "from plugin.server.infrastructure.config_profiles import apply_user_config_profiles;"
        "payload=json.load(sys.stdin);"
        "base=deep_merge(payload['manifest'],payload['runtime']);"
        "merged=apply_user_config_profiles("
        "plugin_id='hearthstone_companion',base_config=base,config_path=Path(sys.argv[1]));"
        "print(json.dumps(merged,ensure_ascii=False))"
    )
    completed = subprocess.run(
        [str(NEKO_PYTHON), "-c", script, str(manifest_path)],
        cwd=NEKO_ROOT,
        input=json.dumps(
            {"manifest": manifest, "runtime": runtime_config},
            ensure_ascii=False,
        ),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=20,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )

    assert completed.returncode == 0, completed.stderr
    merged = json.loads(completed.stdout.strip().splitlines()[-1])
    config = CompanionConfig.from_mapping(merged["hearthstone_companion"])
    assert config.llm_do_not_disturb is expected_do_not_disturb
