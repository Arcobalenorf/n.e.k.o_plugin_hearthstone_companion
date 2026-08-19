from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_PLUGIN_KEYS = {
    "plugin.name",
    "plugin.description",
    "plugin.short_description",
}


def _load_locale(name: str) -> dict[str, str]:
    with (ROOT / "i18n" / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def test_locales_have_identical_keys() -> None:
    english = _load_locale("en.json")
    chinese = _load_locale("zh-CN.json")

    assert english.keys() == chinese.keys()


def test_locales_define_standard_plugin_metadata() -> None:
    for locale in ("en.json", "zh-CN.json"):
        translations = _load_locale(locale)

        assert REQUIRED_PLUGIN_KEYS <= translations.keys()
        assert all(translations[key].strip() for key in REQUIRED_PLUGIN_KEYS)


def test_manifest_initial_read_default_matches_runtime() -> None:
    from hearthstone_companion_under_test.config import CompanionConfig

    with (ROOT / "plugin.toml").open("rb") as handle:
        manifest = tomllib.load(handle)

    runtime_default = CompanionConfig.from_mapping({}).initial_read_max_bytes
    assert manifest["hearthstone_companion"]["initial_read_max_bytes"] == runtime_default


def test_panel_literal_translation_keys_exist() -> None:
    translations = _load_locale("en.json")
    panel_source = (ROOT / "ui" / "panel.tsx").read_text(encoding="utf-8")
    literal_keys = set(re.findall(r'\bt\("([A-Za-z0-9_.-]+)"', panel_source))

    assert literal_keys <= translations.keys()


def test_package_versions_match() -> None:
    with (ROOT / "plugin.toml").open("rb") as handle:
        manifest = tomllib.load(handle)
    with (ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)
    with (ROOT / "uv.lock").open("rb") as handle:
        lock = tomllib.load(handle)

    locked_project = next(
        package for package in lock["package"] if package["name"] == "hearthstone-companion"
    )
    assert manifest["plugin"]["version"] == project["project"]["version"] == locked_project["version"]
