from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

EVIDENCE_SCHEMA = "hearthstone_release_chain_evidence_v1"
MATRIX_SCHEMA = "hearthstone_neko_isolated_answer_matrix_v6"
PROBE_SCHEMA = "hearthstone_neko_answer_probe_v7"
RUNTIME_SCHEMA = "hearthstone_neko_runtime_manifest_v2"
RUNTIME_ASSET_MANIFEST_SCHEMA = "neko_chat_runtime_assets_v1"
RUNTIME_ASSET_MANIFEST_PATH = Path(__file__).with_name("neko_runtime_assets.json")
RUNTIME_ASSET_SOURCE_PATH = "frontend/react-neko-chat"
EVIDENCE_DIRECTORY = Path(".github") / "e2e-evidence"
MATRIX_CLEANUP_KEYS = frozenset(
    {"all_processes_stopped", "all_ports_released", "runtime_removed"}
)
LANE_CLEANUP_KEYS = frozenset(
    {"probe_stopped", "main_stopped", "memory_stopped", "ports_released", "storage_removed"}
)
PROBE_CLEANUP_KEYS = frozenset(
    {
        "plugin_stopped",
        "tools_cleared",
        "service_stopped",
        "temporary_files_removed",
        "lifecycle_proxy_stopped",
        "lifecycle_proxy_port_released",
    }
)
PYTHON_IDENTITY_KEYS = frozenset(
    {
        "implementation",
        "version",
        "cache_tag",
        "executable_sha256",
        "base_executable_sha256",
        "environment_sha256",
        "distribution_count",
        "environment_file_count",
    }
)
CHAT_ASSET_NAMES = frozenset({"neko-chat-window.iife.js", "neko-chat-window.css"})
LIFECYCLE_PROXY_SELECTOR_SHA256 = "593b5822800884f547f906ba4af5f1ebeb9936c5b5096b7c2511bb729ebd75c6"
PASSIVE_CONTEXT_CONTRACT_SHA256 = "ebf0ea3d4fabec6a472a107440233e4622412086cb0e17e77b56e3b7ea761264"
PASSIVE_EVIDENCE_KEYS = frozenset(
    {
        "status",
        "contract_sha256",
        "observed_after_activation",
        "observed_before_submit",
        "envelope_verified",
        "fact_verified",
        "fact_sha256",
        "fact_count",
        "match_id",
        "mode",
        "round",
        "segment",
        "coalesce_key_sha256",
        "semantic_fingerprint",
        "forwarded_sequence",
        "observation_count",
        "no_later_invalidation",
        "reason_codes",
    }
)
PROBE_PRIVACY_FLAGS = frozenset(
    {
        "raw_log_emitted",
        "absolute_path_emitted",
        "question_emitted",
        "answer_text_emitted",
        "player_identity_emitted",
        "role_name_emitted",
        "endpoint_emitted",
    }
)
PROBE_PRIVACY_COUNT_KEYS = frozenset(
    {"questions", "answers", "player_identities", "role_names", "absolute_paths", "endpoints", "raw_log_fragments"}
)
MATRIX_PRIVACY_FLAGS = frozenset(
    {"absolute_path_emitted", "input_log_path_emitted", "role_name_emitted"}
)
MATRIX_PRIVACY_COUNT_KEYS = frozenset({"absolute_paths", "input_log_paths", "role_names"})
EXPECTED_CHECKPOINTS = {
    "constructed_round_v1": ("constructed_round", "constructed", 11),
    "constructed_opponent_v1": ("constructed_opponent", "constructed", 11),
    "bg_shop_v1": ("bg_shop", "battlegrounds", 2),
    "bg_upgrade_blocked_v1": ("bg_upgrade_blocked", "battlegrounds", 3),
    "bg_upgrade_affordable_v1": ("bg_upgrade_affordable", "battlegrounds", 3),
}
_RAW_POWER_LOG_RE = re.compile(
    r"(?:\b(?:PowerTaskList|GameState)\.DebugPrint(?:Power|Game)\(\)\s+-\s+|"
    r"\b(?:TAG_CHANGE\s+Entity=|FULL_ENTITY\s+-\s+(?:Creating|Updating)\b|"
    r"SHOW_ENTITY\s+-\s+Updating\b|HIDE_ENTITY\s+-\s+Entity=))"
)
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"(?i)(?<![A-Za-z0-9])(?:[A-Z]:[\\/]|\\\\[^\\/\s]+[\\/])")
_POSIX_ABSOLUTE_PATH_RE = re.compile(r"(?<![A-Za-z0-9:])/(?!/)[^/\s]+(?:/[^/\s]*)*")
EXPECTED_CASE_TO_TOOL = {
    "constructed_round_v1": "hearthstone_current_turn",
    "constructed_opponent_v1": "hearthstone_live_state",
    "bg_shop_v1": "hearthstone_live_state",
    "bg_upgrade_blocked_v1": "hearthstone_live_state",
    "bg_upgrade_affordable_v1": "hearthstone_live_state",
}
EXPECTED_LIFECYCLE_EDGES = {
    "constructed_started_v1": "started",
    "constructed_ended_v1": "ended",
}
EXPECTED_NEKO_REVISION = "50e23ac5403fcddc96e0dfb9fc78075f32a2428e"


class EvidenceError(ValueError):
    pass


def _is_sha256(value: Any) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_git_object_id(value: Any) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) in {40, 64}
        and all(character in "0123456789abcdef" for character in value)
    )


def _expected_runtime_asset_contract() -> dict[str, Any]:
    try:
        raw_manifest = RUNTIME_ASSET_MANIFEST_PATH.read_bytes()
        manifest = json.loads(raw_manifest)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError("runtime_asset_manifest_invalid") from exc
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema",
        "neko_revision",
        "runtime",
        "source",
        "assets",
    }:
        raise EvidenceError("runtime_asset_manifest_invalid")
    runtime = manifest.get("runtime")
    source = manifest.get("source")
    assets = manifest.get("assets")
    if (
        manifest.get("schema") != RUNTIME_ASSET_MANIFEST_SCHEMA
        or manifest.get("neko_revision") != EXPECTED_NEKO_REVISION
        or not isinstance(runtime, dict)
        or set(runtime)
        != {
            "export_method",
            "root_git_tree",
            "source_file_count",
            "final_file_count",
            "final_sha256",
        }
        or runtime.get("export_method") != "git_archive_tracked_files_v1"
        or not _is_git_object_id(runtime.get("root_git_tree"))
        or not _positive_int(runtime.get("source_file_count"))
        or not _positive_int(runtime.get("final_file_count"))
        or not _is_sha256(runtime.get("final_sha256"))
        or not isinstance(source, dict)
        or set(source)
        != {
            "path",
            "git_tree",
            "node_major",
            "install_command",
            "build_command",
        }
        or source.get("path") != RUNTIME_ASSET_SOURCE_PATH
        or not _is_git_object_id(source.get("git_tree"))
        or not isinstance(source.get("node_major"), int)
        or int(source["node_major"]) <= 0
        or source.get("install_command") != "npm ci"
        or source.get("build_command") != "npm run build"
        or not isinstance(assets, dict)
        or set(assets) != CHAT_ASSET_NAMES
    ):
        raise EvidenceError("runtime_asset_manifest_invalid")
    for raw_asset in assets.values():
        if (
            not isinstance(raw_asset, dict)
            or set(raw_asset) != {"bytes", "sha256"}
            or not _positive_int(raw_asset.get("bytes"))
            or not _is_sha256(raw_asset.get("sha256"))
        ):
            raise EvidenceError("runtime_asset_manifest_invalid")
    return {
        "schema": RUNTIME_ASSET_MANIFEST_SCHEMA,
        "manifest_sha256": hashlib.sha256(raw_manifest).hexdigest(),
        "neko_revision": EXPECTED_NEKO_REVISION,
        "runtime": dict(runtime),
        "source": dict(source),
        "files": {name: dict(assets[name]) for name in sorted(assets)},
    }


def _source_paths(project_root: Path) -> tuple[str, ...]:
    try:
        completed = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=project_root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EvidenceError("source_inventory_unavailable") from exc
    if completed.returncode != 0:
        raise EvidenceError("source_inventory_unavailable")
    try:
        values = completed.stdout.decode("utf-8").split("\0")
    except UnicodeDecodeError as exc:
        raise EvidenceError("source_inventory_invalid") from exc
    excluded = EVIDENCE_DIRECTORY.as_posix() + "/"
    paths = tuple(
        sorted(
            {
                value.replace("\\", "/")
                for value in values
                if value and not value.replace("\\", "/").startswith(excluded)
            }
        )
    )
    if not paths:
        raise EvidenceError("source_inventory_empty")
    return paths


def source_fingerprint(
    project_root: Path,
    *,
    paths: Iterable[str] | None = None,
) -> str:
    root = project_root.resolve(strict=True)
    candidates = tuple(paths) if paths is not None else _source_paths(root)
    digest = hashlib.sha256()
    seen: set[str] = set()
    for raw_relative in sorted(candidates):
        relative = raw_relative.replace("\\", "/")
        if (
            not relative
            or relative in seen
            or relative.startswith("/")
            or any(part in {"", ".", ".."} for part in relative.split("/"))
            or relative.startswith(EVIDENCE_DIRECTORY.as_posix() + "/")
        ):
            raise EvidenceError("source_inventory_invalid")
        seen.add(relative)
        path = root.joinpath(*relative.split("/"))
        try:
            if path.is_symlink() or not path.is_file():
                raise EvidenceError("source_inventory_invalid")
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
            content = resolved.read_bytes().replace(b"\r\n", b"\n")
        except (OSError, ValueError) as exc:
            raise EvidenceError("source_inventory_invalid") from exc
        encoded_path = relative.encode("utf-8")
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    if not seen:
        raise EvidenceError("source_inventory_empty")
    return digest.hexdigest()


def _mapping(value: Any, reason: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvidenceError(reason)
    return value


def _required_true_mapping(value: Any, required_keys: frozenset[str], reason: str) -> None:
    mapping = _mapping(value, reason)
    if set(mapping) != required_keys or any(item is not True for item in mapping.values()):
        raise EvidenceError(reason)


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_privacy(
    value: Any,
    *,
    flags: frozenset[str],
    count_keys: frozenset[str],
    reason: str,
) -> None:
    privacy = _mapping(value, reason)
    if set(privacy) != flags | {"scan_completed", "source_counts"}:
        raise EvidenceError(reason)
    counts = _mapping(privacy.get("source_counts"), reason)
    if (
        privacy.get("scan_completed") is not True
        or any(privacy.get(flag) is not False for flag in flags)
        or set(counts) != count_keys
        or any(not _nonnegative_int(count) for count in counts.values())
    ):
        raise EvidenceError(reason)


def _report_strings(value: Any) -> Iterable[str]:
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            yield item
        elif isinstance(item, Mapping):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)


def _validate_no_sensitive_strings(value: Any) -> None:
    if any(
        _RAW_POWER_LOG_RE.search(item)
        or _WINDOWS_ABSOLUTE_PATH_RE.search(item)
        or _POSIX_ABSOLUTE_PATH_RE.search(item)
        for item in _report_strings(value)
    ):
        raise EvidenceError("evidence_privacy_invalid")


def _validate_readiness(value: Any, *, callback_required: bool) -> None:
    readiness = _mapping(value, "probe_readiness_invalid")
    required_true = (
        "real_e2e_enabled",
        "health_verified",
        "isolation_verified",
        "role_available",
        "tools_registered",
        "official_plugin_service",
    )
    role_hash = readiness.get("role_hash")
    if (
        any(readiness.get(field) is not True for field in required_true)
        or (callback_required and readiness.get("callback_capability_verified") is not True)
        or not isinstance(role_hash, str)
        or re.fullmatch(r"[0-9a-f]{16}", role_hash) is None
    ):
        raise EvidenceError("probe_readiness_invalid")


def _validate_checkpoint(value: Any, *, case_id: str) -> Mapping[str, Any]:
    checkpoint = _mapping(value, "checkpoint_invalid")
    expected = EXPECTED_CHECKPOINTS.get(case_id)
    if expected is None:
        expected = (case_id.split("_v", 1)[0], "constructed", None)
    expected_kind, expected_mode, expected_round = expected
    round_number = checkpoint.get("round")
    if (
        set(checkpoint) != {"kind", "line", "game_number", "mode", "round"}
        or checkpoint.get("kind") != expected_kind
        or checkpoint.get("mode") != expected_mode
        or not _positive_int(checkpoint.get("line"))
        or not _positive_int(checkpoint.get("game_number"))
        or not _nonnegative_int(round_number)
        or (expected_round is not None and round_number != expected_round)
    ):
        raise EvidenceError("checkpoint_invalid")
    return checkpoint


def _validate_query_isolation(value: Any) -> None:
    isolation = _mapping(value, "query_isolation_invalid")
    if (
        set(isolation)
        != {
            "fresh_host_instance",
            "lifecycle_suppressed_before_session",
            "selector_sha256",
            "expected_suppressed_count",
            "lifecycle_stage",
            "suppressed_count",
            "proxy_fatal_count",
            "proxy_running_before_query",
            "quiet_after_capture",
        }
        or isolation.get("fresh_host_instance") is not True
        or isolation.get("lifecycle_suppressed_before_session") is not True
        or isolation.get("selector_sha256") != LIFECYCLE_PROXY_SELECTOR_SHA256
        or not _positive_int(isolation.get("expected_suppressed_count"))
        or isolation.get("expected_suppressed_count") != 1
        or isolation.get("lifecycle_stage") != "resumed"
        or not _positive_int(isolation.get("suppressed_count"))
        or isolation.get("suppressed_count") != 1
        or not _nonnegative_int(isolation.get("proxy_fatal_count"))
        or isolation.get("proxy_fatal_count") != 0
        or isolation.get("proxy_running_before_query") is not True
        or isolation.get("quiet_after_capture") is not True
    ):
        raise EvidenceError("query_isolation_invalid")


def _validate_lifecycle_edge(value: Any, *, case_id: str, checkpoint_line: int) -> None:
    edge = _mapping(value, "lifecycle_edge_invalid")
    pre_line = edge.get("pre_line")
    post_line = edge.get("post_line")
    pre_bytes = edge.get("pre_bytes")
    post_bytes = edge.get("post_bytes")
    appended_bytes = edge.get("appended_bytes")
    expected_pre_stage = "resumed" if case_id == "constructed_ended_v1" else ""
    expected_pre_count = 1 if expected_pre_stage else 0
    if (
        edge.get("incremental_append") is not True
        or not _positive_int(pre_line)
        or not _positive_int(post_line)
        or post_line <= pre_line
        or post_line != checkpoint_line
        or not _positive_int(pre_bytes)
        or not _positive_int(post_bytes)
        or post_bytes <= pre_bytes
        or not _positive_int(appended_bytes)
        or appended_bytes != post_bytes - pre_bytes
        or not _nonnegative_int(edge.get("pre_submission_count"))
        or edge.get("pre_submission_count") != expected_pre_count
        or edge.get("pre_stage") != expected_pre_stage
    ):
        raise EvidenceError("lifecycle_edge_invalid")


def _validate_passive_evidence(value: Any, *, checkpoint: Mapping[str, Any]) -> None:
    passive = _mapping(value, "passive_bundle_invalid")
    semantic_fingerprint = passive.get("semantic_fingerprint")
    if (
        set(passive) != PASSIVE_EVIDENCE_KEYS
        or passive.get("status") != "VERIFIED"
        or passive.get("contract_sha256") != PASSIVE_CONTEXT_CONTRACT_SHA256
        or passive.get("observed_after_activation") is not True
        or passive.get("observed_before_submit") is not True
        or passive.get("envelope_verified") is not True
        or passive.get("fact_verified") is not True
        or not _is_sha256(passive.get("fact_sha256"))
        or not _positive_int(passive.get("fact_count"))
        or not _positive_int(passive.get("match_id"))
        or passive.get("mode") != checkpoint.get("mode")
        or passive.get("round") != checkpoint.get("round")
        or passive.get("segment") != "core"
        or not _is_sha256(passive.get("coalesce_key_sha256"))
        or not isinstance(semantic_fingerprint, str)
        or re.fullmatch(r"[0-9a-f]{16}", semantic_fingerprint) is None
        or not _positive_int(passive.get("forwarded_sequence"))
        or not _positive_int(passive.get("observation_count"))
        or passive.get("no_later_invalidation") is not True
        or passive.get("reason_codes") != []
    ):
        raise EvidenceError("passive_bundle_invalid")


def _validate_python_identity(value: Any) -> Mapping[str, Any]:
    identity = _mapping(value, "matrix_runtime_invalid")
    if (
        set(identity) != PYTHON_IDENTITY_KEYS
        or not all(
            isinstance(identity.get(field), str) and bool(identity.get(field))
            for field in ("implementation", "version", "cache_tag")
        )
        or not all(
            _is_sha256(identity.get(field))
            for field in (
                "executable_sha256",
                "base_executable_sha256",
                "environment_sha256",
            )
        )
        or not _positive_int(identity.get("distribution_count"))
        or not _positive_int(identity.get("environment_file_count"))
    ):
        raise EvidenceError("matrix_runtime_invalid")
    return identity


def _validate_runtime(value: Any) -> None:
    runtime = _mapping(value, "matrix_runtime_invalid")
    neko_source = _mapping(runtime.get("neko_source"), "matrix_runtime_invalid")
    source_export = _mapping(runtime.get("source_export"), "matrix_runtime_invalid")
    python_runtime = _mapping(runtime.get("python"), "matrix_runtime_invalid")
    before_identity = _validate_python_identity(python_runtime.get("before"))
    after_identity = _validate_python_identity(python_runtime.get("after"))
    assets = _mapping(runtime.get("chat_assets"), "matrix_runtime_invalid")
    expected_assets = _expected_runtime_asset_contract()
    expected_runtime = _mapping(expected_assets.get("runtime"), "matrix_runtime_invalid")
    if (
        runtime.get("schema") != RUNTIME_SCHEMA
        or runtime.get("isolated_mirror") is not True
        or runtime.get("stable") is not True
        or set(source_export) != {"method", "revision", "root_tree", "file_count"}
        or source_export.get("method") != expected_runtime.get("export_method")
        or source_export.get("revision") != EXPECTED_NEKO_REVISION
        or source_export.get("root_tree") != expected_runtime.get("root_git_tree")
        or source_export.get("file_count") != expected_runtime.get("source_file_count")
        or assets != expected_assets
        or set(neko_source)
        != {
            "expected_revision",
            "revision_before",
            "revision_after",
            "clean_before",
            "clean_after",
            "stable",
            "release_compatible",
        }
        or neko_source.get("expected_revision") != EXPECTED_NEKO_REVISION
        or neko_source.get("revision_before") != EXPECTED_NEKO_REVISION
        or neko_source.get("revision_after") != EXPECTED_NEKO_REVISION
        or neko_source.get("clean_before") is not True
        or neko_source.get("clean_after") is not True
        or neko_source.get("stable") is not True
        or neko_source.get("release_compatible") is not True
        or not all(
            isinstance(runtime.get(field), str) and bool(runtime.get(field))
            for field in ("app_version", "sdk_version")
        )
        or runtime.get("sha256_before") != expected_runtime.get("final_sha256")
        or runtime.get("sha256_after") != runtime.get("sha256_before")
        or runtime.get("file_count_before") != expected_runtime.get("final_file_count")
        or runtime.get("file_count_after") != runtime.get("file_count_before")
        or python_runtime.get("stable") is not True
        or before_identity != after_identity
    ):
        raise EvidenceError("matrix_runtime_invalid")


def _validate_callback_proof(
    probe: Mapping[str, Any],
    *,
    expected_tool: str,
) -> None:
    proofs = probe.get("tool_callback_proofs")
    if not isinstance(proofs, list) or len(proofs) != 1:
        raise EvidenceError("callback_proof_invalid")
    proof = _mapping(proofs[0], "callback_proof_invalid")
    required_true = (
        "registration_source_verified",
        "remote_registration_verified",
        "callback_target_verified",
        "exact_once",
        "call_id_present",
    )
    output = _mapping(proof.get("output_contract"), "callback_proof_invalid")
    if (
        proof.get("tool_name") != expected_tool
        or proof.get("proof_kind") != "registered_callback_probe"
        or any(proof.get(field) is not True for field in required_true)
        or proof.get("status") != "completed"
        or proof.get("is_error") is not False
        or output.get("fact_verified") is not True
    ):
        raise EvidenceError("callback_proof_invalid")


def _validate_query_lane(lane: Mapping[str, Any], *, case_id: str) -> None:
    if lane.get("status") != "PASS":
        raise EvidenceError("query_chain_not_passed")
    _required_true_mapping(lane.get("cleanup"), LANE_CLEANUP_KEYS, "query_cleanup_incomplete")
    probe = _mapping(lane.get("probe"), "query_probe_invalid")
    if (
        probe.get("schema") != PROBE_SCHEMA
        or probe.get("lane") != "query"
        or probe.get("status") != "PASS"
    ):
        raise EvidenceError("query_probe_invalid")
    _validate_readiness(probe.get("readiness"), callback_required=True)
    _validate_privacy(
        probe.get("privacy"),
        flags=PROBE_PRIVACY_FLAGS,
        count_keys=PROBE_PRIVACY_COUNT_KEYS,
        reason="probe_privacy_invalid",
    )
    _required_true_mapping(probe.get("cleanup"), PROBE_CLEANUP_KEYS, "query_cleanup_incomplete")
    _validate_callback_proof(probe, expected_tool=EXPECTED_CASE_TO_TOOL[case_id])
    cases = probe.get("cases")
    if not isinstance(cases, list) or len(cases) != 1:
        raise EvidenceError("query_case_invalid")
    case = _mapping(cases[0], "query_case_invalid")
    checkpoint = _validate_checkpoint(case.get("checkpoint"), case_id=case_id)
    _validate_query_isolation(case.get("query_isolation"))
    route = _mapping(case.get("route"), "query_case_invalid")
    _validate_passive_evidence(route.get("passive_context"), checkpoint=checkpoint)
    if (
        case.get("case_id") != case_id
        or case.get("status") != "PASS"
    ):
        raise EvidenceError("query_case_invalid")


def _validate_lifecycle_lane(
    lane: Mapping[str, Any],
    *,
    case_id: str,
    expected_stage: str,
) -> None:
    if lane.get("status") != "PASS":
        raise EvidenceError("lifecycle_submission_not_passed")
    _required_true_mapping(lane.get("cleanup"), LANE_CLEANUP_KEYS, "lifecycle_cleanup_incomplete")
    probe = _mapping(lane.get("probe"), "lifecycle_probe_invalid")
    if (
        probe.get("schema") != PROBE_SCHEMA
        or probe.get("lane") != "lifecycle"
        or probe.get("status") != "PASS"
    ):
        raise EvidenceError("lifecycle_probe_invalid")
    _validate_readiness(probe.get("readiness"), callback_required=False)
    _validate_privacy(
        probe.get("privacy"),
        flags=PROBE_PRIVACY_FLAGS,
        count_keys=PROBE_PRIVACY_COUNT_KEYS,
        reason="probe_privacy_invalid",
    )
    _required_true_mapping(probe.get("cleanup"), PROBE_CLEANUP_KEYS, "lifecycle_cleanup_incomplete")
    cases = probe.get("cases")
    if not isinstance(cases, list) or len(cases) != 1:
        raise EvidenceError("lifecycle_case_invalid")
    case = _mapping(cases[0], "lifecycle_case_invalid")
    checkpoint = _validate_checkpoint(case.get("checkpoint"), case_id=case_id)
    lifecycle = _mapping(case.get("lifecycle"), "lifecycle_case_invalid")
    if (
        case.get("case_id") != case_id
        or case.get("status") != "PASS"
        or not _positive_int(lifecycle.get("submission_count"))
        or lifecycle.get("submission_count") != 1
        or lifecycle.get("lifecycle_stage") != expected_stage
    ):
        raise EvidenceError("lifecycle_submission_not_passed")
    if case_id in EXPECTED_LIFECYCLE_EDGES:
        _validate_lifecycle_edge(
            case.get("edge"),
            case_id=case_id,
            checkpoint_line=int(checkpoint["line"]),
        )


def validate_matrix(matrix: Mapping[str, Any], *, source_sha256: str) -> None:
    _validate_no_sensitive_strings(matrix)
    if matrix.get("schema") != MATRIX_SCHEMA or matrix.get("status") != "PASS":
        raise EvidenceError("matrix_not_passed")
    _validate_privacy(
        matrix.get("privacy"),
        flags=MATRIX_PRIVACY_FLAGS,
        count_keys=MATRIX_PRIVACY_COUNT_KEYS,
        reason="matrix_privacy_invalid",
    )
    source = _mapping(matrix.get("source"), "matrix_source_invalid")
    if (
        source.get("stable") is not True
        or source.get("sha256_before") != source_sha256
        or source.get("sha256_after") != source_sha256
    ):
        raise EvidenceError("matrix_source_invalid")
    _validate_runtime(matrix.get("runtime"))
    _required_true_mapping(matrix.get("cleanup"), MATRIX_CLEANUP_KEYS, "matrix_cleanup_incomplete")
    coverage = _mapping(matrix.get("tool_callback_coverage"), "callback_coverage_invalid")
    if coverage.get("all_verified") is not True:
        raise EvidenceError("callback_coverage_invalid")

    cases = matrix.get("cases")
    if not isinstance(cases, list) or [item.get("case_id") for item in cases if isinstance(item, Mapping)] != list(
        EXPECTED_CASE_TO_TOOL
    ):
        raise EvidenceError("matrix_cases_invalid")
    for raw_case in cases:
        case = _mapping(raw_case, "matrix_cases_invalid")
        case_id = str(case.get("case_id") or "")
        if case.get("status") != "PASS":
            raise EvidenceError("matrix_cases_invalid")
        lanes = _mapping(case.get("lanes"), "matrix_cases_invalid")
        _validate_query_lane(_mapping(lanes.get("query"), "query_probe_invalid"), case_id=case_id)
        _validate_lifecycle_lane(
            _mapping(lanes.get("lifecycle"), "lifecycle_probe_invalid"),
            case_id=case_id,
            expected_stage="resumed",
        )

    edges = matrix.get("lifecycle_edges")
    if not isinstance(edges, list) or [item.get("case_id") for item in edges if isinstance(item, Mapping)] != list(
        EXPECTED_LIFECYCLE_EDGES
    ):
        raise EvidenceError("lifecycle_edges_invalid")
    for raw_edge in edges:
        edge = _mapping(raw_edge, "lifecycle_edges_invalid")
        case_id = str(edge.get("case_id") or "")
        if edge.get("status") != "PASS":
            raise EvidenceError("lifecycle_edges_invalid")
        _required_true_mapping(edge.get("cleanup"), LANE_CLEANUP_KEYS, "lifecycle_cleanup_incomplete")
        probe = _mapping(edge.get("probe"), "lifecycle_probe_invalid")
        synthetic_lane = {
            "status": edge.get("status"),
            "cleanup": edge.get("cleanup"),
            "probe": probe,
        }
        _validate_lifecycle_lane(
            synthetic_lane,
            case_id=case_id,
            expected_stage=EXPECTED_LIFECYCLE_EDGES[case_id],
        )


def _versions(project_root: Path) -> tuple[str, str]:
    try:
        plugin = tomllib.loads((project_root / "plugin.toml").read_text(encoding="utf-8"))
        project = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
        return str(plugin["plugin"]["version"]), str(project["project"]["version"])
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise EvidenceError("release_version_unavailable") from exc


def write_release_evidence(
    project_root: Path,
    matrix: Mapping[str, Any],
    output_path: Path,
) -> dict[str, Any]:
    root = project_root.resolve(strict=True)
    source_sha256 = source_fingerprint(root)
    validate_matrix(matrix, source_sha256=source_sha256)
    plugin_version, project_version = _versions(root)
    if plugin_version != project_version:
        raise EvidenceError("release_version_mismatch")
    evidence = {
        "schema": EVIDENCE_SCHEMA,
        "plugin_version": plugin_version,
        "neko_revision": EXPECTED_NEKO_REVISION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_sha256": source_sha256,
        "matrix": matrix,
    }
    destination = output_path.resolve(strict=False)
    expected_parent = (root / EVIDENCE_DIRECTORY).resolve(strict=False)
    if destination.parent != expected_parent or destination.name != f"v{plugin_version}.json":
        raise EvidenceError("evidence_path_invalid")
    destination.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(evidence, ensure_ascii=False, separators=(",", ":")) + "\n"
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(encoded)
        temporary = Path(handle.name)
    try:
        temporary.replace(destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return evidence


def validate_release_evidence(
    project_root: Path,
    evidence_path: Path,
    *,
    tag: str,
) -> dict[str, Any]:
    root = project_root.resolve(strict=True)
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError("evidence_unavailable") from exc
    evidence = _mapping(evidence, "evidence_invalid")
    _validate_no_sensitive_strings(evidence)
    plugin_version, project_version = _versions(root)
    source_sha256 = source_fingerprint(root)
    if (
        evidence.get("schema") != EVIDENCE_SCHEMA
        or plugin_version != project_version
        or evidence.get("plugin_version") != plugin_version
        or evidence.get("neko_revision") != EXPECTED_NEKO_REVISION
        or tag != f"v{plugin_version}"
        or evidence.get("source_sha256") != source_sha256
    ):
        raise EvidenceError("evidence_invalid")
    validate_matrix(
        _mapping(evidence.get("matrix"), "evidence_invalid"),
        source_sha256=source_sha256,
    )
    return dict(evidence)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate deterministic real N.E.K.O chain evidence.")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    try:
        validate_release_evidence(
            Path(args.project_root),
            Path(args.evidence),
            tag=str(args.tag),
        )
    except EvidenceError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
