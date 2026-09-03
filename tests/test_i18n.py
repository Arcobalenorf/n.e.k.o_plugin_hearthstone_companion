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


def test_do_not_disturb_help_matches_the_disabled_default() -> None:
    english = _load_locale("en.json")["setup.doNotDisturb.help"]
    chinese = _load_locale("zh-CN.json")["setup.doNotDisturb.help"]

    assert "Off by default" in english
    assert "Turn it on" in english
    assert "Enabled by default" not in english
    assert "默认关闭" in chinese
    assert "开启后" in chinese
    assert "默认开启" not in chinese


def test_manifest_initial_read_default_matches_runtime() -> None:
    from hearthstone_companion_under_test.config import CompanionConfig

    with (ROOT / "plugin.toml").open("rb") as handle:
        manifest = tomllib.load(handle)

    runtime_default = CompanionConfig.from_mapping({}).initial_read_max_bytes
    assert manifest["hearthstone_companion"]["initial_read_max_bytes"] == runtime_default


def test_manifest_enables_game_state_questions_by_default() -> None:
    from hearthstone_companion_under_test.config import CompanionConfig

    with (ROOT / "plugin.toml").open("rb") as handle:
        manifest = tomllib.load(handle)

    assert CompanionConfig.from_mapping({}).llm_data_consent is True
    assert manifest["hearthstone_companion"]["llm_data_consent"] is True
    assert CompanionConfig.from_mapping({}).llm_do_not_disturb is False
    assert manifest["hearthstone_companion"]["llm_commentary_enabled"] is True
    assert "llm_lifecycle_enabled" not in CompanionConfig.from_mapping({}).to_dict()
    assert "llm_lifecycle_enabled" not in manifest["hearthstone_companion"]
    assert "llm_do_not_disturb" not in manifest["hearthstone_companion"]

    with (ROOT / "config.example.toml").open("rb") as handle:
        runtime_template = tomllib.load(handle)["hearthstone_companion"]
    assert runtime_template["llm_commentary_enabled"] is True
    assert "llm_do_not_disturb" not in runtime_template


def test_panel_literal_translation_keys_exist() -> None:
    translations = _load_locale("en.json")
    panel_source = (ROOT / "ui" / "panel.tsx").read_text(encoding="utf-8")
    literal_keys = set(re.findall(r'\bt\("([A-Za-z0-9_.-]+)"', panel_source))

    assert literal_keys <= translations.keys()


def test_diagnostic_export_uses_the_hosted_file_bridge() -> None:
    panel_source = (ROOT / "ui" / "panel.tsx").read_text(encoding="utf-8")

    assert "FileDownload" in panel_source
    assert "path={diagnosticExport.path}" in panel_source
    assert "window.atob" not in panel_source
    assert "URL.createObjectURL" not in panel_source


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
    version = manifest["plugin"]["version"]
    entry_source = (ROOT / "__init__.py").read_text(encoding="utf-8")
    assert version == project["project"]["version"] == locked_project["version"]
    assert f'_PLUGIN_VERSION = "{version}"' in entry_source


def test_network_user_agent_matches_package_version() -> None:
    with (ROOT / "plugin.toml").open("rb") as handle:
        version = tomllib.load(handle)["plugin"]["version"]
    catalog_source = (ROOT / "card_catalog.py").read_text(encoding="utf-8")

    assert f'"NEKO-Hearthstone-Companion/{version} "' in catalog_source


def test_primary_setup_is_offline_first_and_keeps_log_details_in_diagnostics() -> None:
    panel_source = (ROOT / "ui" / "panel.tsx").read_text(encoding="utf-8")
    setup_start = panel_source.index('<Card title={t("sections.setup.title")}>')
    setup_end = panel_source.index('{game.mode === "battlegrounds"', setup_start)
    setup_source = panel_source[setup_start:setup_end]

    assert 't("setup.offlineHelp")' in setup_source
    assert 'actionAvailable("save_settings")' in setup_source
    assert "monitorReady" not in setup_source
    assert "prepare_power_log" not in setup_source
    assert "settings.logPath" not in setup_source
    assert "actions.enable_companion.withDoNotDisturb" in panel_source
    assert "actions.enable_companion.withoutDoNotDisturb" in panel_source
    assert "llm_data_consent: true" in panel_source
    assert "llm_do_not_disturb: false" in panel_source
    assert "value?.llm_do_not_disturb === true" in panel_source
    assert "value?.llm_data_consent !== false" in panel_source
    assert "llm_lifecycle_enabled" not in panel_source
    assert "llm_commentary_enabled" not in panel_source


def test_panel_consent_and_do_not_disturb_controls_are_independent() -> None:
    panel_source = (ROOT / "ui" / "panel.tsx").read_text(encoding="utf-8")
    consent_start = panel_source.index("function setConsent(enabled: boolean)")
    do_not_disturb_start = panel_source.index("function setDoNotDisturb(enabled: boolean)")
    consent_source = panel_source[consent_start:do_not_disturb_start]
    do_not_disturb_end = panel_source.index("const sourceState", do_not_disturb_start)
    do_not_disturb_source = panel_source[do_not_disturb_start:do_not_disturb_end]

    assert "llm_data_consent: enabled" in consent_source
    assert "llm_do_not_disturb" not in consent_source
    assert "llm_do_not_disturb: enabled" in do_not_disturb_source
    assert "llm_data_consent" not in do_not_disturb_source
    assert '"llm_lifecycle_reactions_enabled": bool(self.cfg.llm_data_consent)' in (
        ROOT / "__init__.py"
    ).read_text(encoding="utf-8")


def test_panel_only_submits_settings_fields_changed_by_the_user() -> None:
    panel_source = (ROOT / "ui" / "panel.tsx").read_text(encoding="utf-8")

    assert "const [draftPatch, setDraftPatch] = useState<Partial<SettingsDraft>>({})" in panel_source
    assert "const draftDirty = Object.keys(draftPatch).length > 0" in panel_source
    assert "setDraftPatch((current) => ({ ...current, ...patch }))" in panel_source
    assert "const submitted = { ...draftPatch }" in panel_source
    assert '"save_settings",\n      submitted,' in panel_source
    assert 'runAction("save_settings", draft,' not in panel_source
    assert "if (remaining[key] === submitted[key]) delete remaining[key]" in panel_source


def test_successful_action_is_not_reclassified_when_followup_refresh_fails() -> None:
    panel_source = (ROOT / "ui" / "panel.tsx").read_text(encoding="utf-8")

    assert "let refreshed = true" in panel_source
    assert 't("warnings.refreshAfterAction")' in panel_source
    assert "return { ok: true, refreshed, result }" in panel_source
    assert "preserveDraftOnCleanRef.current = !outcome.refreshed" in panel_source


def test_panel_auto_refresh_is_serial_silent_and_preserves_dirty_drafts() -> None:
    panel_source = (ROOT / "ui" / "panel.tsx").read_text(encoding="utf-8")

    assert "window.setTimeout(refreshLater, 500)" in panel_source
    assert "window.clearTimeout(timerId)" in panel_source
    assert "setInterval" not in panel_source
    assert "refreshInFlightRef" in panel_source
    assert "await refreshContext(false)" in panel_source
    assert "await refreshContext(true)" in panel_source
    assert "Background refresh is best-effort" in panel_source
    assert "if (draftDirty) return" not in panel_source
    assert "...asSettingsDraft(safeState.settings),\n      ...draftPatch," in panel_source
    assert "}, [safeState.settings, draftPatch])" in panel_source
    assert "if (preserveDraftOnCleanRef.current)" in panel_source
    assert "preserveDraftOnCleanRef.current = false" in panel_source
    assert "if (logPathDirty) return" in panel_source


def test_panel_surfaces_observed_hero_choices_and_duos_teammate() -> None:
    panel_source = (ROOT / "ui" / "panel.tsx").read_text(encoding="utf-8")

    assert "hero_choices?: BattlegroundsHeroChoice[]" in panel_source
    assert '(battlegrounds.phase || phase) === "hero_select" || heroChoiceRows.length > 0' in panel_source
    assert 't("battlegroundsHeroChoices.observedHelp")' in panel_source
    assert 'row.is_teammate ? t("battlegroundsLobby.teammate")' in panel_source


def test_custom_log_path_has_an_independent_save_action_in_diagnostics() -> None:
    panel_source = (ROOT / "ui" / "panel.tsx").read_text(encoding="utf-8")

    assert 'runAction("save_settings", { log_path: normalized }, successKey, false)' in panel_source
    assert "if (announce) {\n      setFailure(\"\")\n      setNotice(\"\")" in panel_source
    assert "const [logPathDirty, setLogPathDirty] = useState(false)" in panel_source
    assert '!logPathDirty || !actionAvailable("save_settings")' in panel_source
    assert 't("actions.save_log_path.label")' in panel_source
    assert 't("actions.restore_auto_log_path.label")' in panel_source


def test_quickstart_keeps_technical_log_details_out_of_primary_flow() -> None:
    quickstart = (ROOT / "docs" / "quickstart.md").read_text(encoding="utf-8")

    assert "不需要先打开炉石" in quickstart
    assert "%LOCALAPPDATA%" not in quickstart
    assert "```ini" not in quickstart
    assert "monitor_running" not in quickstart
    assert "source_state" not in quickstart
