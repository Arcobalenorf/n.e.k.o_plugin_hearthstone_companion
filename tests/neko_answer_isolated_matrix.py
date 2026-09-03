from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.error import URLError
from urllib.request import urlopen

from neko_answer_eval import supported_case_ids
from neko_answer_probe import (
    E2E_ATTESTATION_FILE,
    E2E_INSTANCE_PREFIX,
    LIFECYCLE_EDGE_STAGES,
    _reserve_loopback_ports,
)
from neko_answer_probe import (
    SCHEMA as PROBE_SCHEMA,
)
from owned_process import (
    communicate_with_tree_cleanup as _communicate_with_tree_cleanup,
)
from owned_process import (
    process_group_options as _process_group_options,
)
from owned_process import remove_owned_directory as _remove_owned_directory
from owned_process import (
    spawn_owned_process as _spawn_isolated_process,
)
from owned_process import (
    stop_owned_process_tree as _stop_process_tree,
)

SCHEMA = "hearthstone_neko_isolated_answer_matrix_v6"
RUNTIME_SCHEMA = "hearthstone_neko_runtime_manifest_v2"
_HOST_READY_TIMEOUT_SECONDS = 90.0
_PROBE_TIMEOUT_SECONDS = 240.0
_CASE_TOOL = {
    "constructed_round_v1": "hearthstone_current_turn",
    "constructed_opponent_v1": "hearthstone_live_state",
    "bg_shop_v1": "hearthstone_live_state",
    "bg_upgrade_blocked_v1": "hearthstone_live_state",
    "bg_upgrade_affordable_v1": "hearthstone_live_state",
}
_CHAT_ASSET_NAMES = ("neko-chat-window.iife.js", "neko-chat-window.css")
_CHAT_ASSET_MANIFEST_SCHEMA = "neko_chat_runtime_assets_v1"
_CHAT_ASSET_SOURCE_PATH = "frontend/react-neko-chat"
_RUNTIME_ASSET_MANIFEST_PATH = Path(__file__).with_name("neko_runtime_assets.json")
_RUNTIME_TOP_LEVEL_EXCLUDES = {
    ".agent",
    ".git",
    ".github",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "docker",
    "docs",
    "frontend",
    "logs",
    "pkgbuild",
    "scripts",
    "specs",
    "tests",
}
_RUNTIME_ANY_LEVEL_EXCLUDES = {
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
}
_KNOWN_FAILURE_CODES = frozenset(
    {
        "storage_template_invalid",
        "neko_runtime_unavailable",
        "runtime_assets_invalid",
        "runtime_assets_manifest_invalid",
        "runtime_source_export_failed",
        "memory_not_ready",
        "main_not_ready",
        "probe_output_invalid",
        "probe_process_failed",
    }
)
_HOST_DIAGNOSTIC_PATTERNS = {
    "upstream_rate_limited": re.compile(
        r"(?<![A-Za-z0-9])429(?![A-Za-z0-9])|too many requests|rate.?limit|quota exceeded",
        re.IGNORECASE,
    ),
    "empty_completion": re.compile(r"empty completion", re.IGNORECASE),
    "forced_final_empty": re.compile(r"forced-finalize empty completion", re.IGNORECASE),
    "tool_iteration_cap": re.compile(r"tool iteration cap", re.IGNORECASE),
    "llm_connection_exhausted": re.compile(
        r"LLM_CONNECTION_EXHAUSTED|所有重试均未产生文本回复|LLM连接失败",
        re.IGNORECASE,
    ),
    "upstream_auth_rejected": re.compile(
        r"(?<![A-Za-z0-9])401(?![A-Za-z0-9])|invalid api key|authentication failed",
        re.IGNORECASE,
    ),
}
_HOST_DIAGNOSTIC_MAX_FILES = 8
_HOST_DIAGNOSTIC_MAX_BYTES = 16 * 1024 * 1024
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"(?i)(?<![A-Za-z0-9])(?:[A-Z]:[\\/]|\\\\[^\\/\s]+[\\/])")
_POSIX_ABSOLUTE_PATH_RE = re.compile(r"(?<![A-Za-z0-9:])/(?:[^/\s]+/)+[^/\s]*")


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _prepare_storage(template: Path, destination: Path) -> None:
    config_source = template / "config"
    core_config_source = config_source / "core_config.json"
    characters_source = config_source / "characters.json"
    if not core_config_source.is_file() or not characters_source.is_file():
        raise ValueError("storage_template_invalid")
    config_destination = destination / "config"
    config_destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(core_config_source, config_destination / core_config_source.name)
    shutil.copy2(characters_source, config_destination / characters_source.name)
    core_config_path = destination / "config" / "core_config.json"
    try:
        core_config = json.loads(core_config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("storage_template_invalid") from exc
    if not isinstance(core_config, dict):
        raise ValueError("storage_template_invalid")
    core_config["disableTts"] = True
    _write_json(core_config_path, core_config)

    root = str(destination.resolve(strict=False))
    _write_json(
        destination / "state" / "root_state.json",
        {
            "version": 1,
            "mode": "normal",
            "current_root": root,
            "last_known_good_root": root,
            "last_migration_source": "",
            "last_migration_backup": "",
            "last_migration_result": "e2e_isolation",
            "last_successful_boot_at": "",
            "legacy_cleanup_pending": False,
        },
    )
    _write_json(
        destination / "state" / "storage_policy.json",
        {
            "version": 1,
            "anchor_root": root,
            "selected_root": root,
            "selection_source": "e2e_isolation",
            "cloudsave_strategy": "fixed_anchor",
            "first_run_completed": True,
            "updated_at": "",
        },
    )
    for name in ("memory", "logs", "appdata", "localappdata"):
        (destination / name).mkdir(parents=True, exist_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _runtime_tree_fingerprint(root: Path) -> dict[str, Any]:
    resolved_root = root.resolve(strict=True)
    digest = hashlib.sha256()
    file_count = 0
    for path in sorted(resolved_root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(resolved_root)
        if (
            (relative.parts and relative.parts[0] in _RUNTIME_TOP_LEVEL_EXCLUDES)
            or any(part in _RUNTIME_ANY_LEVEL_EXCLUDES for part in relative.parts)
            or (relative.parts and relative.parts[0].startswith(".tmp-"))
        ):
            continue
        if path.is_symlink():
            raise ValueError("runtime_identity_unavailable")
        if not path.is_file():
            continue
        encoded_path = relative.as_posix().encode("utf-8")
        size = path.stat().st_size
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
        digest.update(size.to_bytes(8, "big"))
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        file_count += 1
    if file_count < 1:
        raise ValueError("runtime_identity_unavailable")
    return {"sha256": digest.hexdigest(), "file_count": file_count}


def _runtime_versions(root: Path) -> dict[str, str]:
    resolved_root = root.resolve(strict=True)
    declarations = {
        "app_version": ("config/application.py", "APP_VERSION"),
        "sdk_version": ("plugin/_types/version.py", "SDK_VERSION"),
    }
    versions: dict[str, str] = {}
    for key, (relative, variable) in declarations.items():
        try:
            text = (resolved_root / relative).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValueError("runtime_version_unavailable") from exc
        match = re.search(
            rf'^\s*{re.escape(variable)}\s*=\s*["\']([^"\']+)["\']\s*$',
            text,
            flags=re.MULTILINE,
        )
        if match is None or not re.fullmatch(r"\d+\.\d+\.\d+", match.group(1)):
            raise ValueError("runtime_version_unavailable")
        versions[key] = match.group(1)
    return versions


def _neko_source_identity(root: Path) -> dict[str, Any]:
    resolved_root = root.resolve(strict=True)
    try:
        revision_result = subprocess.run(
            ["git", "-C", str(resolved_root), "rev-parse", "--verify", "HEAD"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10.0,
            encoding="ascii",
        )
        status_result = subprocess.run(
            [
                "git",
                "-C",
                str(resolved_root),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignore-submodules=none",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10.0,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        raise ValueError("neko_source_identity_unavailable") from exc
    revision = revision_result.stdout.strip().lower()
    if (
        revision_result.returncode != 0
        or status_result.returncode != 0
        or re.fullmatch(r"[0-9a-f]{40}", revision) is None
    ):
        raise ValueError("neko_source_identity_unavailable")
    return {
        "revision": revision,
        "clean": not bool(status_result.stdout.strip()),
    }


def _python_runtime_identity(python_executable: Path) -> dict[str, Any]:
    script = r"""
import hashlib
import json
import platform
import sys
import sysconfig
from importlib.metadata import distributions
from pathlib import Path

def sha256(path_value):
    digest = hashlib.sha256()
    with Path(path_value).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

packages = {}
for distribution in distributions():
    name = str(distribution.metadata.get("Name") or "").strip().lower()
    normalized = name.replace("_", "-").replace(".", "-")
    if normalized:
        packages[normalized] = str(distribution.version)
metadata_records = {}
metadata_roots = {
    Path(value).resolve()
    for key in ("purelib", "platlib")
    if (value := sysconfig.get_paths().get(key))
}
for root in metadata_roots:
    for pattern in (
        "*.dist-info/RECORD",
        "*.dist-info/METADATA",
        "*.egg-info/PKG-INFO",
        "*.egg-link",
    ):
        for path in root.glob(pattern):
            if path.is_file():
                key = f"{root.name}/{path.relative_to(root).as_posix()}"
                metadata_records[key] = sha256(path)
digest = hashlib.sha256()
for name in sorted(packages):
    record = f"distribution:{name}=={packages[name]}".encode("utf-8")
    digest.update(len(record).to_bytes(8, "big"))
    digest.update(record)
for key in sorted(metadata_records):
    record = f"metadata:{key}:{metadata_records[key]}".encode("utf-8")
    digest.update(len(record).to_bytes(8, "big"))
    digest.update(record)
base_executable = getattr(sys, "_base_executable", "") or sys.executable
print(json.dumps({
    "implementation": platform.python_implementation(),
    "version": ".".join(str(part) for part in sys.version_info[:3]),
    "cache_tag": str(sys.implementation.cache_tag or ""),
    "executable_sha256": sha256(sys.executable),
    "base_executable_sha256": sha256(base_executable),
    "environment_sha256": digest.hexdigest(),
    "distribution_count": len(packages),
    "environment_file_count": len(metadata_records),
}, sort_keys=True))
"""
    try:
        completed = subprocess.run(
            [str(python_executable), "-c", script],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30.0,
            encoding="utf-8",
        )
        payload = json.loads(completed.stdout)
    except (OSError, UnicodeError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        raise ValueError("python_identity_unavailable") from exc
    if completed.returncode != 0 or not isinstance(payload, dict):
        raise ValueError("python_identity_unavailable")
    return payload


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git_object_id(source_root: Path, object_name: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "--verify", object_name],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10.0,
            encoding="ascii",
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        raise ValueError("runtime_source_export_failed") from exc
    object_id = completed.stdout.strip().lower()
    if completed.returncode != 0 or re.fullmatch(r"[0-9a-f]{40,64}", object_id) is None:
        raise ValueError("runtime_source_export_failed")
    return object_id


def _runtime_member_path(name: str) -> PurePosixPath | None:
    relative = PurePosixPath(name)
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("runtime_source_export_failed")
    if (
        relative.parts[0] in _RUNTIME_TOP_LEVEL_EXCLUDES
        or relative.parts[0].startswith(".tmp-")
        or any(part in _RUNTIME_ANY_LEVEL_EXCLUDES for part in relative.parts)
    ):
        return None
    return relative


def _export_tracked_runtime(
    source_root: Path,
    destination_root: Path,
    revision: str,
) -> dict[str, Any]:
    root_tree = _git_object_id(source_root, f"{revision}^{{tree}}")
    archive_handle = tempfile.NamedTemporaryFile(
        dir=destination_root.parent,
        prefix=".neko-runtime-source-",
        suffix=".tar",
        delete=False,
    )
    archive_path = Path(archive_handle.name)
    archive_handle.close()
    try:
        try:
            completed = subprocess.run(
                [
                    "git",
                    "-C",
                    str(source_root),
                    "archive",
                    "--format=tar",
                    f"--output={archive_path}",
                    revision,
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120.0,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("runtime_source_export_failed") from exc
        if completed.returncode != 0:
            raise ValueError("runtime_source_export_failed")

        destination_root.mkdir(parents=True, exist_ok=False)
        file_count = 0
        with tarfile.open(archive_path, mode="r:") as archive:
            for member in archive:
                relative = _runtime_member_path(member.name)
                if relative is None:
                    continue
                destination = destination_root.joinpath(*relative.parts)
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise ValueError("runtime_source_export_failed")
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError("runtime_source_export_failed")
                destination.parent.mkdir(parents=True, exist_ok=True)
                with source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output)
                if os.name != "nt":
                    destination.chmod(member.mode & 0o777)
                file_count += 1
    except (OSError, tarfile.TarError) as exc:
        raise ValueError("runtime_source_export_failed") from exc
    finally:
        archive_path.unlink(missing_ok=True)
    if file_count < 1 or not (destination_root / "app" / "main_server").is_dir():
        raise ValueError("neko_runtime_unavailable")
    return {
        "method": "git_archive_tracked_files_v1",
        "revision": revision,
        "root_tree": root_tree,
        "file_count": file_count,
    }


def _verified_runtime_assets(
    source_root: Path,
    runtime_assets_dir: Path,
    revision: str,
) -> tuple[dict[str, Path], dict[str, Any]]:
    try:
        manifest_path = _RUNTIME_ASSET_MANIFEST_PATH.resolve(strict=True)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("runtime_assets_manifest_invalid") from exc
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema",
        "neko_revision",
        "runtime",
        "source",
        "assets",
    }:
        raise ValueError("runtime_assets_manifest_invalid")
    runtime = manifest.get("runtime")
    source = manifest.get("source")
    expected_assets = manifest.get("assets")
    if (
        manifest.get("schema") != _CHAT_ASSET_MANIFEST_SCHEMA
        or manifest.get("neko_revision") != revision
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
        or re.fullmatch(r"[0-9a-f]{40,64}", str(runtime.get("root_git_tree") or "")) is None
        or not isinstance(runtime.get("source_file_count"), int)
        or int(runtime["source_file_count"]) <= 0
        or not isinstance(runtime.get("final_file_count"), int)
        or int(runtime["final_file_count"]) <= 0
        or re.fullmatch(r"[0-9a-f]{64}", str(runtime.get("final_sha256") or "")) is None
        or not isinstance(source, dict)
        or set(source)
        != {
            "path",
            "git_tree",
            "node_major",
            "install_command",
            "build_command",
        }
        or source.get("path") != _CHAT_ASSET_SOURCE_PATH
        or not isinstance(source.get("node_major"), int)
        or int(source["node_major"]) <= 0
        or source.get("install_command") != "npm ci"
        or source.get("build_command") != "npm run build"
        or not isinstance(expected_assets, dict)
        or set(expected_assets) != set(_CHAT_ASSET_NAMES)
    ):
        raise ValueError("runtime_assets_manifest_invalid")
    if _git_object_id(source_root, f"{revision}^{{tree}}") != runtime["root_git_tree"]:
        raise ValueError("runtime_assets_manifest_invalid")
    source_tree = _git_object_id(source_root, f"{revision}:{_CHAT_ASSET_SOURCE_PATH}")
    if source.get("git_tree") != source_tree:
        raise ValueError("runtime_assets_manifest_invalid")

    assets: dict[str, Path] = {}
    metadata: dict[str, Any] = {}
    for name in _CHAT_ASSET_NAMES:
        expected = expected_assets.get(name)
        if (
            not isinstance(expected, dict)
            or set(expected) != {"bytes", "sha256"}
            or not isinstance(expected.get("bytes"), int)
            or int(expected["bytes"]) <= 0
            or re.fullmatch(r"[0-9a-f]{64}", str(expected.get("sha256") or "")) is None
        ):
            raise ValueError("runtime_assets_manifest_invalid")
        try:
            candidate = (runtime_assets_dir / name).resolve(strict=True)
        except OSError as exc:
            raise ValueError("runtime_assets_invalid") from exc
        actual = {
            "bytes": candidate.stat().st_size if candidate.is_file() else 0,
            "sha256": _sha256(candidate) if candidate.is_file() else "",
        }
        if candidate.parent != runtime_assets_dir or actual != expected:
            raise ValueError("runtime_assets_invalid")
        assets[name] = candidate
        metadata[name] = actual
    return assets, {
        "schema": str(manifest["schema"]),
        "manifest_sha256": _sha256(manifest_path),
        "neko_revision": revision,
        "runtime": dict(runtime),
        "source": dict(source),
        "files": metadata,
    }


def _prepare_runtime(
    source_root: Path,
    destination_root: Path,
    runtime_assets_dir: Path,
    *,
    revision: str,
) -> dict[str, Any]:
    source_root = source_root.resolve(strict=True)
    runtime_assets_dir = runtime_assets_dir.resolve(strict=True)
    assets, asset_contract = _verified_runtime_assets(
        source_root,
        runtime_assets_dir,
        revision,
    )
    source_export = _export_tracked_runtime(source_root, destination_root, revision)
    expected_runtime = asset_contract["runtime"]
    if (
        source_export["method"] != expected_runtime["export_method"]
        or source_export["root_tree"] != expected_runtime["root_git_tree"]
        or source_export["file_count"] != expected_runtime["source_file_count"]
    ):
        raise ValueError("runtime_source_export_failed")

    destination_assets = destination_root / "static" / "react" / "neko-chat"
    destination_assets.mkdir(parents=True, exist_ok=True)
    for name, source in assets.items():
        destination = destination_assets / name
        if destination.exists():
            destination.unlink()
        shutil.copy2(source, destination)
        if {
            "bytes": destination.stat().st_size,
            "sha256": _sha256(destination),
        } != asset_contract["files"][name]:
            raise ValueError("runtime_assets_invalid")
    final_fingerprint = _runtime_tree_fingerprint(destination_root)
    if final_fingerprint != {
        "sha256": expected_runtime["final_sha256"],
        "file_count": expected_runtime["final_file_count"],
    }:
        raise ValueError("runtime_source_export_failed")
    return {
        "source_export": source_export,
        "chat_assets": asset_contract,
    }


def _isolated_environment(
    storage_root: Path,
    *,
    instance_id: str,
    attestation_token: str,
    ports: tuple[int, ...],
) -> dict[str, str]:
    (
        main_port,
        memory_port,
        plugin_port,
        session_port,
        agent_port,
        analyze_port,
        message_rpc_port,
        message_pub_port,
        message_ingest_port,
        _proxy_ingress_port,
    ) = ports
    env = dict(os.environ)
    env.update(
        {
            "PYTHONUTF8": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "APPDATA": str(storage_root / "appdata"),
            "LOCALAPPDATA": str(storage_root / "localappdata"),
            "NEKO_STORAGE_SELECTED_ROOT": str(storage_root),
            "NEKO_STORAGE_ANCHOR_ROOT": str(storage_root),
            "NEKO_USER_DATA_DIR": str(storage_root),
            "NEKO_CLOUDSAVE_DISABLED": "e2e_isolation",
            "NEKO_DO_NOT_TRACK": "1",
            "DO_NOT_TRACK": "1",
            "NEKO_INSTANCE_ID": instance_id,
            "HEARTHSTONE_E2E_ATTESTATION_TOKEN": attestation_token,
            "NEKO_MAIN_SERVER_PORT": str(main_port),
            "NEKO_MEMORY_SERVER_PORT": str(memory_port),
            "NEKO_USER_PLUGIN_SERVER_PORT": str(plugin_port),
            "NEKO_ZMQ_SESSION_PUB_PORT": str(session_port),
            "NEKO_ZMQ_AGENT_PUSH_PORT": str(agent_port),
            "NEKO_ZMQ_ANALYZE_PUSH_PORT": str(analyze_port),
            "NEKO_MESSAGE_PLANE_ZMQ_RPC_ENDPOINT": f"tcp://127.0.0.1:{message_rpc_port}",
            "NEKO_MESSAGE_PLANE_ZMQ_PUB_ENDPOINT": f"tcp://127.0.0.1:{message_pub_port}",
            "NEKO_MESSAGE_PLANE_ZMQ_INGEST_ENDPOINT": (f"tcp://127.0.0.1:{message_ingest_port}"),
        }
    )
    return env


def _wait_health(url: str, *, instance_id: str, service: str) -> None:
    deadline = time.monotonic() + _HOST_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=1.0) as response:
                payload = json.loads(response.read(65_536).decode("utf-8"))
        except (OSError, URLError, TimeoutError, UnicodeError, json.JSONDecodeError):
            time.sleep(0.25)
            continue
        if (
            isinstance(payload, Mapping)
            and payload.get("status") == "ok"
            and payload.get("service") == service
            and payload.get("instance_id") == instance_id
        ):
            return
        time.sleep(0.25)
    raise RuntimeError(f"{service}_not_ready")


def _cleanup_runtime_directory(
    runtime_directory: Path,
    *,
    timeout: float = 10.0,
) -> bool:
    return _remove_owned_directory(
        runtime_directory,
        required_prefix="neko-hearthstone-runtime-",
        timeout=timeout,
    )


def _reason_code(exc: BaseException, *, default: str) -> str:
    if isinstance(exc, subprocess.TimeoutExpired):
        return "probe_timeout"
    if isinstance(exc, OSError):
        return "isolated_case_io_error"
    code = str(exc)
    if code in _KNOWN_FAILURE_CODES:
        return code
    return default


def _error_payload(reason_code: str) -> dict[str, Any]:
    return {
        "schema": PROBE_SCHEMA,
        "status": "ERROR",
        "reason_code": reason_code,
        "cases": [],
    }


def _classify_host_logs(storage_root: Path) -> dict[str, Any]:
    """Return bounded, low-cardinality host failure evidence without log text."""

    counts = {name: 0 for name in _HOST_DIAGNOSTIC_PATTERNS}
    files = sorted((storage_root / "logs").glob("N.E.K.O_Main*.log"))[:_HOST_DIAGNOSTIC_MAX_FILES]
    bytes_scanned = 0
    truncated = False
    for path in files:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    encoded_size = len(line.encode("utf-8", errors="replace"))
                    if bytes_scanned + encoded_size > _HOST_DIAGNOSTIC_MAX_BYTES:
                        truncated = True
                        break
                    bytes_scanned += encoded_size
                    for name, pattern in _HOST_DIAGNOSTIC_PATTERNS.items():
                        if pattern.search(line):
                            counts[name] += 1
        except OSError:
            return {
                "scan_completed": False,
                "reason_code": "host_log_unreadable",
                "log_files_scanned": 0,
                "bytes_scanned": 0,
                "truncated": False,
                "counts": counts,
            }
        if truncated:
            break
    return {
        "scan_completed": True,
        "log_files_scanned": len(files),
        "bytes_scanned": bytes_scanned,
        "truncated": truncated,
        "counts": counts,
        "observed_categories": sorted(name for name, count in counts.items() if count),
    }


def _matrix_report_strings(value: Any):
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            yield item
        elif isinstance(item, Mapping):
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)


def _matrix_contains_source(values: tuple[str, ...], source: str) -> bool:
    normalized = source.casefold().replace("\\", "/").strip()
    if not normalized:
        return False
    normalized_values = tuple(value.casefold().replace("\\", "/") for value in values)
    if any(normalized == value for value in normalized_values):
        return True
    if len(normalized) >= 8:
        return any(normalized in value for value in normalized_values)
    token = re.compile(rf"(?<!\w){re.escape(normalized)}(?!\w)")
    return any(token.search(value) is not None for value in normalized_values)


def _finalize_matrix_privacy(
    report: dict[str, Any],
    *,
    absolute_paths: set[str],
    input_log_paths: set[str],
    role_names: set[str],
) -> bool:
    values = tuple(_matrix_report_strings(report))
    leaks = {
        "absolute_path_emitted": bool(
            any(_matrix_contains_source(values, source) for source in absolute_paths)
            or any(_WINDOWS_ABSOLUTE_PATH_RE.search(value) or _POSIX_ABSOLUTE_PATH_RE.search(value) for value in values)
        ),
        "input_log_path_emitted": any(_matrix_contains_source(values, source) for source in input_log_paths),
        "role_name_emitted": any(_matrix_contains_source(values, source) for source in role_names),
    }
    privacy = {
        **leaks,
        "scan_completed": True,
        "source_counts": {
            "absolute_paths": len(absolute_paths),
            "input_log_paths": len(input_log_paths),
            "role_names": len(role_names),
        },
    }
    if not any(leaks.values()):
        report["privacy"] = privacy
        return True

    cleanup = report.get("cleanup")
    safe_cleanup = dict(cleanup) if isinstance(cleanup, Mapping) else {}
    report.clear()
    report.update(
        {
            "schema": SCHEMA,
            "status": "ERROR",
            "reason_code": "serialized_matrix_privacy_leak",
            "cleanup": safe_cleanup,
            "privacy": privacy,
        }
    )
    return False


def _ports_released(ports: tuple[int, ...]) -> bool:
    for port in ports:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.2)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return False
        except OSError:
            continue
        finally:
            probe.close()
    return True


def _probe_command(
    *,
    project_root: Path,
    neko_root: Path,
    python_executable: Path,
    role: str,
    instance_id: str,
    ports: tuple[int, ...],
    case: tuple[str, str, str],
    lane: str,
    edge_pre_line: int = 0,
) -> list[str]:
    (
        main_port,
        memory_port,
        plugin_port,
        session_port,
        agent_port,
        analyze_port,
        message_rpc_port,
        message_pub_port,
        message_ingest_port,
        proxy_ingress_port,
    ) = ports
    case_id, line, log_path = case
    command = [
        str(python_executable),
        str(project_root / "tests" / "neko_answer_probe.py"),
        "--enable-real-e2e",
        "--single-case",
        "--lane",
        lane,
        "--base-url",
        f"http://127.0.0.1:{main_port}",
        "--plugin-base-url",
        f"http://127.0.0.1:{plugin_port}",
        "--isolated-instance-id",
        instance_id,
        "--role",
        role,
        "--neko-root",
        str(neko_root),
        "--neko-python",
        str(python_executable),
        "--memory-port",
        str(memory_port),
        "--session-pub-port",
        str(session_port),
        "--agent-push-port",
        str(agent_port),
        "--plugin-agent-push-port",
        str(proxy_ingress_port),
        "--analyze-push-port",
        str(analyze_port),
        "--message-plane-rpc-endpoint",
        f"tcp://127.0.0.1:{message_rpc_port}",
        "--message-plane-pub-endpoint",
        f"tcp://127.0.0.1:{message_pub_port}",
        "--message-plane-ingest-endpoint",
        f"tcp://127.0.0.1:{message_ingest_port}",
        "--case",
        case_id,
        line,
        log_path,
    ]
    if edge_pre_line:
        command.extend(("--edge-pre-line", str(edge_pre_line)))
    return command


def _run_isolated_case(
    *,
    project_root: Path,
    neko_root: Path,
    python_executable: Path,
    storage_template: Path,
    role: str,
    case: tuple[str, str, str],
    lane: str,
    edge_pre_line: int = 0,
) -> tuple[dict[str, Any], dict[str, bool]]:
    if lane not in {"lifecycle", "query"}:
        raise ValueError("invalid_probe_lane")
    ports = _reserve_loopback_ports(10)
    instance_id = f"{E2E_INSTANCE_PREFIX}{uuid.uuid4().hex}"
    attestation_token = secrets.token_hex(32)
    main_process: subprocess.Popen[bytes] | None = None
    memory_process: subprocess.Popen[bytes] | None = None
    probe_process: subprocess.Popen[bytes] | None = None
    cleanup = {
        "probe_stopped": True,
        "main_stopped": True,
        "memory_stopped": True,
        "ports_released": True,
        "storage_removed": False,
    }
    payload: dict[str, Any] = _error_payload("isolated_case_failed")
    storage_root = Path(tempfile.mkdtemp(prefix="neko-hearthstone-answer-case-")).resolve(strict=True)
    try:
        _prepare_storage(storage_template, storage_root)
        _write_json(
            storage_root / "state" / E2E_ATTESTATION_FILE,
            {
                "schema": "hearthstone_e2e_attestation_v1",
                "instance_id": instance_id,
                "token": attestation_token,
            },
        )
        env = _isolated_environment(
            storage_root,
            instance_id=instance_id,
            attestation_token=attestation_token,
            ports=ports,
        )
        memory_process = _spawn_isolated_process(
            [str(python_executable), "-m", "app.memory_server"],
            cwd=neko_root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **_process_group_options(),
        )
        cleanup["memory_stopped"] = False
        _wait_health(
            f"http://127.0.0.1:{ports[1]}/health",
            instance_id=instance_id,
            service="memory",
        )
        main_process = _spawn_isolated_process(
            [str(python_executable), "-m", "app.main_server"],
            cwd=neko_root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **_process_group_options(),
        )
        cleanup["main_stopped"] = False
        _wait_health(
            f"http://127.0.0.1:{ports[0]}/health",
            instance_id=instance_id,
            service="main",
        )
        probe_process = _spawn_isolated_process(
            _probe_command(
                project_root=project_root,
                neko_root=neko_root,
                python_executable=python_executable,
                role=role,
                instance_id=instance_id,
                ports=ports,
                case=case,
                lane=lane,
                edge_pre_line=edge_pre_line,
            ),
            cwd=project_root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **_process_group_options(),
        )
        cleanup["probe_stopped"] = False
        stdout, _stderr = _communicate_with_tree_cleanup(
            probe_process,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
        output_lines = stdout.decode("utf-8", errors="replace").splitlines()
        payload = json.loads(output_lines[-1]) if output_lines else {}
        if not isinstance(payload, dict):
            raise RuntimeError("probe_output_invalid")
        if probe_process.returncode != 0 and not (
            payload.get("schema") == PROBE_SCHEMA
            and payload.get("status") in {"FAIL", "ERROR"}
        ):
            raise RuntimeError("probe_process_failed")
    except (
        OSError,
        RuntimeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
        subprocess.TimeoutExpired,
    ) as exc:
        payload = _error_payload(_reason_code(exc, default="isolated_case_failed"))
    finally:
        cleanup["probe_stopped"] = _stop_process_tree(probe_process)
        cleanup["main_stopped"] = _stop_process_tree(main_process)
        cleanup["memory_stopped"] = _stop_process_tree(memory_process)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not _ports_released(ports):
            time.sleep(0.1)
        cleanup["ports_released"] = _ports_released(ports)
        payload["host_diagnostics"] = _classify_host_logs(storage_root)
        if all(cleanup[key] for key in ("probe_stopped", "main_stopped", "memory_stopped")):
            cleanup["storage_removed"] = _remove_owned_directory(
                storage_root,
                required_prefix="neko-hearthstone-answer-case-",
            )
    return payload, cleanup


def _verified_callback_tools(cases: object) -> list[str]:
    if not isinstance(cases, list):
        return []
    verified: set[str] = set()
    for item in cases:
        if not isinstance(item, Mapping):
            continue
        lanes = item.get("lanes")
        query = lanes.get("query") if isinstance(lanes, Mapping) else None
        probe = query.get("probe") if isinstance(query, Mapping) else None
        proofs = probe.get("tool_callback_proofs") if isinstance(probe, Mapping) else None
        if not isinstance(proofs, list):
            continue
        for proof in proofs:
            if not isinstance(proof, Mapping):
                continue
            output_contract = proof.get("output_contract")
            if (
                proof.get("tool_name") in _CASE_TOOL.values()
                and proof.get("proof_kind") == "registered_callback_probe"
                and proof.get("registration_source_verified") is True
                and proof.get("remote_registration_verified") is True
                and proof.get("callback_target_verified") is True
                and proof.get("exact_once") is True
                and proof.get("call_id_present") is True
                and proof.get("status") == "completed"
                and proof.get("is_error") is False
                and isinstance(output_contract, Mapping)
                and output_contract.get("fact_verified") is True
            ):
                verified.add(str(proof["tool_name"]))
    return sorted(verified)


def _run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    from release_evidence import (
        EXPECTED_NEKO_REVISION,
        EvidenceError,
        source_fingerprint,
    )

    report: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "ERROR",
        "isolation": "fresh_main_memory_browser_plugin_and_storage_per_lane",
        "cases": [],
        "lifecycle_edges": [],
        "tool_callback_coverage": {
            "required_tools": [],
            "verified_tools": [],
            "all_verified": False,
        },
        "cleanup": {
            "all_processes_stopped": True,
            "all_ports_released": True,
            "runtime_removed": True,
        },
        "runtime": {},
        "source": {},
        "execution": {
            "started_at_utc": _utc_now_iso(),
            "completed_at_utc": "",
        },
    }
    raw_cases = [tuple(str(item) for item in raw) for raw in (args.case or [])]
    raw_edges = [tuple(str(item) for item in raw) for raw in (getattr(args, "lifecycle_edge", None) or [])]
    single_case = bool(getattr(args, "single_case", False))
    case_ids = tuple(case[0] for case in raw_cases)
    if (single_case and len(case_ids) != 1) or (not single_case and case_ids != supported_case_ids()):
        report["reason_code"] = "incomplete_case_matrix"
        return report, 1
    if any(not Path(case[2]).is_file() for case in raw_cases):
        report["reason_code"] = "no_real_logs"
        return report, 1
    expected_edge_ids = tuple(LIFECYCLE_EDGE_STAGES)
    edge_ids = tuple(edge[0] for edge in raw_edges)
    if not single_case and edge_ids != expected_edge_ids:
        report["reason_code"] = "incomplete_lifecycle_edge_matrix"
        return report, 1
    for edge in raw_edges:
        if (
            len(edge) != 4
            or edge[0] not in LIFECYCLE_EDGE_STAGES
            or not edge[1].isdigit()
            or not edge[2].isdigit()
            or int(edge[1]) <= 0
            or int(edge[2]) <= int(edge[1])
            or not Path(edge[3]).is_file()
        ):
            report["reason_code"] = "invalid_lifecycle_edge"
            return report, 1
    project_root = Path(__file__).resolve().parents[1]
    try:
        source_before = source_fingerprint(project_root)
    except EvidenceError:
        report["reason_code"] = "source_fingerprint_unavailable"
        return report, 1
    report["source"] = {
        "sha256_before": source_before,
        "sha256_after": "",
        "stable": False,
    }
    neko_root = Path(args.neko_root).resolve(strict=False)
    python_executable = Path(args.neko_python).resolve(strict=False)
    storage_template = Path(args.storage_template).resolve(strict=False)
    if not python_executable.is_file() or not (neko_root / "app").is_dir():
        report["reason_code"] = "neko_runtime_unavailable"
        return report, 1

    try:
        neko_source_before = _neko_source_identity(neko_root)
    except (OSError, ValueError):
        report["reason_code"] = "neko_source_identity_unavailable"
        return report, 1
    release_compatible_source = bool(
        neko_source_before["revision"] == EXPECTED_NEKO_REVISION
        and neko_source_before["clean"] is True
    )
    if not single_case and not release_compatible_source:
        report["reason_code"] = (
            "neko_source_dirty"
            if neko_source_before["revision"] == EXPECTED_NEKO_REVISION
            else "neko_source_revision_mismatch"
        )
        return report, 1

    runtime_assets_value = str(getattr(args, "runtime_assets_dir", "") or "").strip()
    if not runtime_assets_value:
        report["reason_code"] = "runtime_assets_required"
        return report, 1

    runtime_directory: Path | None = None
    runtime_root = neko_root
    runtime_before: dict[str, Any] | None = None
    python_before: dict[str, Any] | None = None
    try:
        runtime_directory = Path(
            tempfile.mkdtemp(
                prefix="neko-hearthstone-runtime-",
                dir=neko_root.parent,
            )
        ).resolve(strict=True)
        runtime_root = runtime_directory / "N.E.K.O"
        try:
            runtime_materials = _prepare_runtime(
                neko_root,
                runtime_root,
                Path(runtime_assets_value),
                revision=str(neko_source_before["revision"]),
            )
        except (OSError, ValueError) as exc:
            report["reason_code"] = _reason_code(
                exc,
                default="runtime_prepare_failed",
            )
            return report, 1

        try:
            runtime_before = _runtime_tree_fingerprint(runtime_root)
            runtime_versions = _runtime_versions(runtime_root)
            python_before = _python_runtime_identity(python_executable)
        except (OSError, ValueError) as exc:
            report["reason_code"] = _reason_code(
                exc,
                default="runtime_identity_unavailable",
            )
            return report, 1
        report["runtime"] = {
            "schema": RUNTIME_SCHEMA,
            "isolated_mirror": True,
            "neko_source": {
                "expected_revision": EXPECTED_NEKO_REVISION,
                "revision_before": neko_source_before["revision"],
                "revision_after": "",
                "clean_before": neko_source_before["clean"],
                "clean_after": False,
                "stable": False,
                "release_compatible": release_compatible_source,
            },
            "source_export": runtime_materials["source_export"],
            **runtime_versions,
            "sha256_before": runtime_before["sha256"],
            "sha256_after": "",
            "file_count_before": runtime_before["file_count"],
            "file_count_after": 0,
            "stable": False,
            "python": {
                "before": python_before,
                "after": {},
                "stable": False,
            },
            "chat_assets": runtime_materials["chat_assets"],
        }

        for case in raw_cases:
            lanes: dict[str, Any] = {}
            for lane in ("lifecycle", "query"):
                try:
                    payload, cleanup = _run_isolated_case(
                        project_root=project_root,
                        neko_root=runtime_root,
                        python_executable=python_executable,
                        storage_template=storage_template,
                        role=str(args.role),
                        case=case,
                        lane=lane,
                    )
                except (
                    OSError,
                    RuntimeError,
                    ValueError,
                    subprocess.TimeoutExpired,
                ) as exc:
                    payload = _error_payload(_reason_code(exc, default="isolated_case_failed"))
                    cleanup = {
                        "probe_stopped": False,
                        "main_stopped": False,
                        "memory_stopped": False,
                        "ports_released": False,
                        "storage_removed": False,
                    }
                lanes[lane] = {
                    "status": payload.get("status"),
                    "probe": payload,
                    "cleanup": cleanup,
                }
                report["cleanup"]["all_processes_stopped"] = bool(
                    report["cleanup"]["all_processes_stopped"]
                    and cleanup.get("probe_stopped")
                    and cleanup.get("main_stopped")
                    and cleanup.get("memory_stopped")
                )
                report["cleanup"]["all_ports_released"] = bool(
                    report["cleanup"]["all_ports_released"] and cleanup.get("ports_released")
                )
            report["cases"].append(
                {
                    "case_id": case[0],
                    "status": ("PASS" if all(item.get("status") == "PASS" for item in lanes.values()) else "FAIL"),
                    "lanes": lanes,
                }
            )
        required_tools = sorted({_CASE_TOOL[case_id] for case_id in case_ids})
        verified_tools = _verified_callback_tools(report["cases"])
        report["tool_callback_coverage"] = {
            "required_tools": required_tools,
            "verified_tools": verified_tools,
            "all_verified": verified_tools == required_tools,
        }
        for edge_id, pre_line, post_line, log_path in raw_edges:
            edge_case = (edge_id, post_line, log_path)
            try:
                payload, cleanup = _run_isolated_case(
                    project_root=project_root,
                    neko_root=runtime_root,
                    python_executable=python_executable,
                    storage_template=storage_template,
                    role=str(args.role),
                    case=edge_case,
                    lane="lifecycle",
                    edge_pre_line=int(pre_line),
                )
            except (
                OSError,
                RuntimeError,
                ValueError,
                subprocess.TimeoutExpired,
            ) as exc:
                payload = _error_payload(_reason_code(exc, default="isolated_case_failed"))
                cleanup = {
                    "probe_stopped": False,
                    "main_stopped": False,
                    "memory_stopped": False,
                    "ports_released": False,
                    "storage_removed": False,
                }
            report["lifecycle_edges"].append(
                {
                    "case_id": edge_id,
                    "status": payload.get("status"),
                    "probe": payload,
                    "cleanup": cleanup,
                }
            )
            report["cleanup"]["all_processes_stopped"] = bool(
                report["cleanup"]["all_processes_stopped"]
                and cleanup.get("probe_stopped")
                and cleanup.get("main_stopped")
                and cleanup.get("memory_stopped")
            )
            report["cleanup"]["all_ports_released"] = bool(
                report["cleanup"]["all_ports_released"] and cleanup.get("ports_released")
            )
    finally:
        if runtime_before is not None and python_before is not None:
            try:
                runtime_after = _runtime_tree_fingerprint(runtime_root)
                python_after = _python_runtime_identity(python_executable)
                neko_source_after = _neko_source_identity(neko_root)
            except (OSError, ValueError):
                report.setdefault("reason_code", "runtime_identity_unavailable")
            else:
                runtime_report = report["runtime"]
                runtime_report["sha256_after"] = runtime_after["sha256"]
                runtime_report["file_count_after"] = runtime_after["file_count"]
                runtime_report["stable"] = runtime_after == runtime_before
                runtime_report["python"]["after"] = python_after
                runtime_report["python"]["stable"] = python_after == python_before
                neko_source_report = runtime_report["neko_source"]
                neko_source_report["revision_after"] = neko_source_after["revision"]
                neko_source_report["clean_after"] = neko_source_after["clean"]
                neko_source_report["stable"] = neko_source_after == neko_source_before
                neko_source_report["release_compatible"] = bool(
                    release_compatible_source and neko_source_report["stable"]
                )
                if (
                    not runtime_report["stable"]
                    or not runtime_report["python"]["stable"]
                    or not neko_source_report["stable"]
                ):
                    report.setdefault("reason_code", "runtime_changed_during_matrix")
        report["execution"]["completed_at_utc"] = _utc_now_iso()
        if runtime_directory is not None:
            report["cleanup"]["runtime_removed"] = bool(
                report["cleanup"]["all_processes_stopped"] and _cleanup_runtime_directory(runtime_directory)
            )

    try:
        source_after = source_fingerprint(project_root)
    except EvidenceError:
        source_after = ""
    report["source"]["sha256_after"] = source_after
    report["source"]["stable"] = bool(source_after and source_after == source_before)
    passed = (
        bool(report["cases"])
        and (
            single_case
            or tuple(item.get("case_id") for item in report["lifecycle_edges"]) == tuple(LIFECYCLE_EDGE_STAGES)
        )
        and report["source"]["stable"]
        and report["runtime"].get("stable") is True
        and report["runtime"].get("isolated_mirror") is True
        and report["runtime"].get("python", {}).get("stable") is True
        and report["runtime"].get("neko_source", {}).get("stable") is True
        and (
            single_case
            or report["runtime"].get("neko_source", {}).get("release_compatible") is True
        )
        and report["tool_callback_coverage"].get("all_verified") is True
        and all(report["cleanup"].values())
        and all(
            item.get("status") == "PASS"
            and all(
                lane.get("status") == "PASS" and all((lane.get("cleanup") or {}).values())
                for lane in (item.get("lanes") or {}).values()
            )
            for item in report["cases"]
        )
        and all(
            item.get("status") == "PASS" and all((item.get("cleanup") or {}).values())
            for item in report["lifecycle_edges"]
        )
    )
    report["status"] = "PASS" if passed else "FAIL"
    return report, 0 if passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run five real N.E.K.O answer checkpoints plus started/ended lifecycle "
            "edges through fresh lane-specific host instances."
        )
    )
    parser.add_argument("--neko-root", required=True)
    parser.add_argument("--neko-python", required=True)
    parser.add_argument("--storage-template", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument(
        "--runtime-assets-dir",
        required=True,
        help=(
            "Directory containing prebuilt N.E.K.O chat JS/CSS. The runner "
            "overlays them into a disposable runtime mirror."
        ),
    )
    parser.add_argument(
        "--evidence-output",
        default="",
        help=(
            "On a complete deterministic chain PASS, write source-bound evidence to "
            ".github/e2e-evidence/v<version>.json."
        ),
    )
    parser.add_argument(
        "--single-case",
        action="store_true",
        help="Run one diagnostic case instead of the complete five-case observation matrix.",
    )
    parser.add_argument(
        "--case",
        action="append",
        nargs=3,
        required=True,
        metavar=("CASE_ID", "LINE", "POWER_LOG"),
    )
    parser.add_argument(
        "--lifecycle-edge",
        action="append",
        nargs=4,
        default=[],
        metavar=("CASE_ID", "PRE_LINE", "POST_LINE", "POWER_LOG"),
        help=(
            "Required for a full observation matrix: constructed_started_v1 and "
            "constructed_ended_v1 real incremental boundaries."
        ),
    )
    args = parser.parse_args()
    report, code = _run(args)
    input_log_paths = {str(Path(str(case[2])).resolve(strict=False)) for case in (args.case or ())}
    input_log_paths.update(str(Path(str(edge[3])).resolve(strict=False)) for edge in (args.lifecycle_edge or ()))
    absolute_paths = set(input_log_paths)
    for raw_path in (
        args.neko_root,
        args.neko_python,
        args.storage_template,
        args.runtime_assets_dir,
        args.evidence_output,
    ):
        text = str(raw_path or "").strip()
        if text:
            absolute_paths.add(str(Path(text).resolve(strict=False)))
    privacy_ok = _finalize_matrix_privacy(
        report,
        absolute_paths=absolute_paths,
        input_log_paths=input_log_paths,
        role_names={str(args.role).strip()} if str(args.role).strip() else set(),
    )
    if not privacy_ok:
        code = 1
    if code == 0 and str(args.evidence_output).strip():
        from release_evidence import EvidenceError, write_release_evidence

        try:
            write_release_evidence(
                Path(__file__).resolve().parents[1],
                report,
                Path(args.evidence_output),
            )
        except EvidenceError as exc:
            report["status"] = "ERROR"
            report["reason_code"] = str(exc)
            code = 1
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return code


if __name__ == "__main__":
    sys.exit(main())
