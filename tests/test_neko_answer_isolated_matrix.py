from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import neko_answer_isolated_matrix as matrix
import owned_process
import pytest
from neko_answer_isolated_matrix import (
    _classify_host_logs,
    _cleanup_runtime_directory,
    _isolated_environment,
    _neko_source_identity,
    _prepare_runtime,
    _prepare_storage,
    _python_runtime_identity,
    _run,
    _run_isolated_case,
    _runtime_tree_fingerprint,
    _runtime_versions,
    _verified_callback_tools,
)
from owned_process import (
    _terminate_windows_process_handles,
)
from owned_process import (
    communicate_with_tree_cleanup as _communicate_with_tree_cleanup,
)
from owned_process import (
    spawn_owned_process as _spawn_isolated_process,
)
from owned_process import (
    stop_owned_process_tree as _stop_process_tree,
)
from owned_process import (
    windows_process_snapshot as _windows_process_snapshot,
)
from release_evidence import EXPECTED_NEKO_REVISION


def test_ports_released_detects_only_active_listeners() -> None:
    listener = matrix.socket.socket(matrix.socket.AF_INET, matrix.socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = int(listener.getsockname()[1])
    try:
        assert matrix._ports_released((port,)) is False
    finally:
        listener.close()

    assert matrix._ports_released((port,)) is True


def test_prepare_storage_copies_configuration_but_starts_with_empty_memory(
    tmp_path: Path,
) -> None:
    template = tmp_path / "template"
    config = template / "config"
    config.mkdir(parents=True)
    (config / "core_config.json").write_text(
        json.dumps({"disableTts": False, "model": "test"}),
        encoding="utf-8",
    )
    (config / "characters.json").write_text("{}", encoding="utf-8")
    (config / "telemetry-private.jsonl").write_text(
        "must-not-be-copied\n",
        encoding="utf-8",
    )
    (template / "memory" / "old-role").mkdir(parents=True)
    destination = tmp_path / "isolated"
    destination.mkdir()

    _prepare_storage(template, destination)

    core_config = json.loads((destination / "config" / "core_config.json").read_text(encoding="utf-8"))
    root_state = json.loads((destination / "state" / "root_state.json").read_text(encoding="utf-8"))
    storage_policy = json.loads((destination / "state" / "storage_policy.json").read_text(encoding="utf-8"))
    assert core_config == {"disableTts": True, "model": "test"}
    assert not (destination / "config" / "telemetry-private.jsonl").exists()
    assert list((destination / "memory").iterdir()) == []
    assert root_state["current_root"] == str(destination.resolve())
    assert storage_policy["selected_root"] == str(destination.resolve())


def test_isolated_environment_routes_every_service_to_the_case_instance(
    tmp_path: Path,
) -> None:
    ports = tuple(range(31001, 31011))

    env = _isolated_environment(
        tmp_path,
        instance_id="hearthstone-e2e-00000000000000000000000000000001",
        attestation_token="a" * 64,
        ports=ports,
    )

    assert env["NEKO_INSTANCE_ID"] == "hearthstone-e2e-00000000000000000000000000000001"
    assert env["HEARTHSTONE_E2E_ATTESTATION_TOKEN"] == "a" * 64
    assert env["NEKO_MAIN_SERVER_PORT"] == "31001"
    assert env["NEKO_MEMORY_SERVER_PORT"] == "31002"
    assert env["NEKO_USER_PLUGIN_SERVER_PORT"] == "31003"
    assert env["NEKO_MESSAGE_PLANE_ZMQ_RPC_ENDPOINT"] == "tcp://127.0.0.1:31007"
    assert env["NEKO_MESSAGE_PLANE_ZMQ_PUB_ENDPOINT"] == "tcp://127.0.0.1:31008"
    assert env["NEKO_MESSAGE_PLANE_ZMQ_INGEST_ENDPOINT"] == "tcp://127.0.0.1:31009"
    assert env["NEKO_STORAGE_SELECTED_ROOT"] == str(tmp_path)
    assert env["NEKO_DO_NOT_TRACK"] == "1"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_host_log_diagnostics_expose_only_bounded_categories(tmp_path: Path) -> None:
    private_text = "private-role private-question private-answer"
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "N.E.K.O_Main_1.log").write_text(
        "\n".join(
            (
                private_text,
                "upstream returned HTTP 429",
                "OmniOfflineClient(openai): empty completion finish_reason=stop",
                "OmniOfflineClient: tool iteration cap 3 reached",
                "LLM_CONNECTION_EXHAUSTED",
            )
        ),
        encoding="utf-8",
    )

    result = _classify_host_logs(tmp_path)

    assert result["scan_completed"] is True
    assert result["counts"] == {
        "upstream_rate_limited": 1,
        "empty_completion": 1,
        "forced_final_empty": 0,
        "tool_iteration_cap": 1,
        "llm_connection_exhausted": 1,
        "upstream_auth_rejected": 0,
    }
    assert private_text not in repr(result)


def _commit_runtime_fixture(source: Path) -> tuple[str, str]:
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / "app" / "main_server").mkdir(parents=True)
    (source / "app" / "main_server" / "__init__.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    frontend = source / "frontend" / "react-neko-chat"
    frontend.mkdir(parents=True)
    (frontend / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (source / ".gitignore").write_text(
        ".env\n.venv/\nconfig/api.py\nstatic/react/neko-chat/\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    revision = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        stdout=subprocess.PIPE,
        encoding="ascii",
    ).stdout.strip()
    frontend_tree = subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "rev-parse",
            f"{revision}:frontend/react-neko-chat",
        ],
        check=True,
        stdout=subprocess.PIPE,
        encoding="ascii",
    ).stdout.strip()
    return revision, frontend_tree


def _write_runtime_asset_manifest(
    path: Path,
    *,
    source: Path,
    revision: str,
    frontend_tree: str,
    assets: Path,
) -> None:
    files: dict[str, object] = {}
    for name in ("neko-chat-window.iife.js", "neko-chat-window.css"):
        payload = (assets / name).read_bytes()
        files[name] = {
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    expected_root = path.parent / f".{path.stem}-expected-runtime"
    source_export = matrix._export_tracked_runtime(source, expected_root, revision)
    try:
        expected_assets = expected_root / "static" / "react" / "neko-chat"
        expected_assets.mkdir(parents=True, exist_ok=True)
        for name in files:
            shutil.copy2(assets / name, expected_assets / name)
        runtime_fingerprint = _runtime_tree_fingerprint(expected_root)
    finally:
        shutil.rmtree(expected_root)
    path.write_text(
        json.dumps(
            {
                "schema": "neko_chat_runtime_assets_v1",
                "neko_revision": revision,
                "runtime": {
                    "export_method": source_export["method"],
                    "root_git_tree": source_export["root_tree"],
                    "source_file_count": source_export["file_count"],
                    "final_file_count": runtime_fingerprint["file_count"],
                    "final_sha256": runtime_fingerprint["sha256"],
                },
                "source": {
                    "path": "frontend/react-neko-chat",
                    "git_tree": frontend_tree,
                    "node_major": 22,
                    "install_command": "npm ci",
                    "build_command": "npm run build",
                },
                "assets": files,
            }
        ),
        encoding="utf-8",
    )


def test_prepare_runtime_exports_only_pinned_tracked_bytes_and_verified_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    revision, frontend_tree = _commit_runtime_fixture(source)
    source_asset_dir = source / "static" / "react" / "neko-chat"
    source_asset_dir.mkdir(parents=True)
    source_js = source_asset_dir / "neko-chat-window.iife.js"
    source_css = source_asset_dir / "neko-chat-window.css"
    source_js.write_text("old-js", encoding="utf-8")
    source_css.write_text("old-css", encoding="utf-8")
    (source / "app" / "main_server" / "__init__.py").write_text(
        "WORKTREE_ONLY = True\n",
        encoding="utf-8",
    )
    (source / ".venv").mkdir()
    (source / ".venv" / "ignored.txt").write_text("ignored", encoding="utf-8")
    (source / ".env").write_text("PRIVATE=1\n", encoding="utf-8")
    (source / "config").mkdir()
    (source / "config" / "api.py").write_text("TOKEN = 'private'\n", encoding="utf-8")
    (source / "untracked.txt").write_text("not committed\n", encoding="utf-8")

    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "neko-chat-window.iife.js").write_text("release-js", encoding="utf-8")
    (assets / "neko-chat-window.css").write_text("release-css", encoding="utf-8")
    manifest = tmp_path / "trusted-runtime-assets.json"
    _write_runtime_asset_manifest(
        manifest,
        source=source,
        revision=revision,
        frontend_tree=frontend_tree,
        assets=assets,
    )
    monkeypatch.setattr(matrix, "_RUNTIME_ASSET_MANIFEST_PATH", manifest)
    destination = tmp_path / "runtime"

    metadata = _prepare_runtime(source, destination, assets, revision=revision)

    assert source_js.read_text(encoding="utf-8") == "old-js"
    assert source_css.read_text(encoding="utf-8") == "old-css"
    assert (destination / "static" / "react" / "neko-chat" / source_js.name).read_text(encoding="utf-8") == "release-js"
    assert (destination / "static" / "react" / "neko-chat" / source_css.name).read_text(
        encoding="utf-8"
    ) == "release-css"
    assert not (destination / ".venv").exists()
    assert not (destination / ".git").exists()
    assert not (destination / ".env").exists()
    assert not (destination / "config" / "api.py").exists()
    assert not (destination / "untracked.txt").exists()
    assert metadata["source_export"]["method"] == "git_archive_tracked_files_v1"
    assert metadata["source_export"]["revision"] == revision
    assert metadata["chat_assets"]["source"]["git_tree"] == frontend_tree
    assert metadata["chat_assets"]["files"][source_js.name]["bytes"] == len("release-js")
    mirrored_runtime = destination / "app" / "main_server" / "__init__.py"
    assert mirrored_runtime.read_text(encoding="utf-8") == "VALUE = 1\n"


def test_prepare_runtime_rejects_assets_not_matching_trusted_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    revision, frontend_tree = _commit_runtime_fixture(source)
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "neko-chat-window.iife.js").write_text("release-js", encoding="utf-8")
    (assets / "neko-chat-window.css").write_text("release-css", encoding="utf-8")
    manifest = tmp_path / "trusted-runtime-assets.json"
    _write_runtime_asset_manifest(
        manifest,
        source=source,
        revision=revision,
        frontend_tree=frontend_tree,
        assets=assets,
    )
    monkeypatch.setattr(matrix, "_RUNTIME_ASSET_MANIFEST_PATH", manifest)

    (assets / "neko-chat-window.iife.js").write_text("different-build", encoding="utf-8")
    (assets / "runtime-manifest.json").write_text(
        json.dumps({"neko_revision": revision}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="^runtime_assets_invalid$"):
        _prepare_runtime(source, tmp_path / "runtime", assets, revision=revision)


def test_prepare_runtime_rejects_manifest_not_bound_to_pinned_frontend_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    revision, frontend_tree = _commit_runtime_fixture(source)
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "neko-chat-window.iife.js").write_text("release-js", encoding="utf-8")
    (assets / "neko-chat-window.css").write_text("release-css", encoding="utf-8")
    manifest = tmp_path / "trusted-runtime-assets.json"
    _write_runtime_asset_manifest(
        manifest,
        source=source,
        revision=revision,
        frontend_tree="0" * len(frontend_tree),
        assets=assets,
    )
    monkeypatch.setattr(matrix, "_RUNTIME_ASSET_MANIFEST_PATH", manifest)

    with pytest.raises(ValueError, match="^runtime_assets_manifest_invalid$"):
        _prepare_runtime(source, tmp_path / "runtime", assets, revision=revision)


def test_runtime_tree_fingerprint_is_path_bound_and_ignores_runtime_outputs(
    tmp_path: Path,
) -> None:
    (tmp_path / "app").mkdir()
    source = tmp_path / "app" / "runtime.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "logs").mkdir()
    generated = tmp_path / "logs" / "runtime.log"
    generated.write_text("first\n", encoding="utf-8")

    first = _runtime_tree_fingerprint(tmp_path)
    generated.write_text("second\n", encoding="utf-8")
    assert _runtime_tree_fingerprint(tmp_path) == first

    source.write_text("VALUE = 2\n", encoding="utf-8")
    assert _runtime_tree_fingerprint(tmp_path) != first


def test_python_runtime_identity_is_bounded_and_path_free() -> None:
    identity = _python_runtime_identity(Path(sys.executable))

    assert identity["implementation"] == "CPython"
    assert str(identity["version"]).startswith(f"{sys.version_info.major}.{sys.version_info.minor}.")
    assert int(identity["distribution_count"]) > 0
    assert int(identity["environment_file_count"]) > 0
    assert all(
        len(str(identity[key])) == 64
        for key in (
            "executable_sha256",
            "base_executable_sha256",
            "environment_sha256",
        )
    )
    assert str(Path(sys.executable).parent) not in repr(identity)


def test_runtime_versions_read_official_host_declarations(tmp_path: Path) -> None:
    application = tmp_path / "config" / "application.py"
    sdk_version = tmp_path / "plugin" / "_types" / "version.py"
    application.parent.mkdir(parents=True)
    sdk_version.parent.mkdir(parents=True)
    application.write_text('APP_VERSION = "0.9.0"\n', encoding="utf-8")
    sdk_version.write_text('SDK_VERSION = "0.1.0"\n', encoding="utf-8")

    assert _runtime_versions(tmp_path) == {
        "app_version": "0.9.0",
        "sdk_version": "0.1.0",
    }


def test_neko_source_identity_reads_head_and_relevant_worktree_cleanliness(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )

    clean = _neko_source_identity(tmp_path)
    assert len(clean["revision"]) == 40
    assert clean["clean"] is True

    (tmp_path / "untracked.txt").write_text("change\n", encoding="utf-8")
    dirty = _neko_source_identity(tmp_path)
    assert dirty["revision"] == clean["revision"]
    assert dirty["clean"] is False


def test_isolated_matrix_rejects_partial_case_sets_before_starting_hosts() -> None:
    report, code = _run(
        SimpleNamespace(
            case=[("constructed_round_v1", "1", "private")],
            neko_root="private",
            neko_python="private",
            storage_template="private",
            role="private",
        )
    )

    assert code == 1
    assert report["status"] == "ERROR"
    assert report["reason_code"] == "incomplete_case_matrix"
    assert "private" not in repr(report)


@pytest.mark.parametrize(
    ("identity", "expected_reason"),
    (
        ({"revision": "0" * 40, "clean": True}, "neko_source_revision_mismatch"),
        (
            {"revision": EXPECTED_NEKO_REVISION, "clean": False},
            "neko_source_dirty",
        ),
    ),
    ids=("wrong-revision", "dirty-worktree"),
)
def test_full_matrix_rejects_unpinned_or_dirty_neko_source_before_host_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity: dict[str, object],
    expected_reason: str,
) -> None:
    log = tmp_path / "Power.log"
    log.write_text("checkpoint\n", encoding="utf-8")
    neko_root = tmp_path / "N.E.K.O"
    (neko_root / "app").mkdir(parents=True)
    cases = [(case_id, "1", str(log)) for case_id in matrix.supported_case_ids()]
    edges = [
        (edge_id, "1", "2", str(log))
        for edge_id in matrix.LIFECYCLE_EDGE_STAGES
    ]
    monkeypatch.setattr(matrix, "_neko_source_identity", lambda _root: identity)

    report, code = _run(
        SimpleNamespace(
            case=cases,
            lifecycle_edge=edges,
            single_case=False,
            neko_root=str(neko_root),
            neko_python=sys.executable,
            storage_template=str(tmp_path / "storage"),
            runtime_assets_dir=str(tmp_path / "assets"),
            role="test-role",
        )
    )

    assert code == 1
    assert report["reason_code"] == expected_reason
    assert report["cases"] == []


def test_matrix_probe_command_forwards_lifecycle_edge_boundaries(
    tmp_path: Path,
) -> None:
    command = matrix._probe_command(
        project_root=tmp_path / "plugin",
        neko_root=tmp_path / "runtime",
        python_executable=Path(sys.executable),
        role="test-role",
        instance_id="hearthstone-e2e-" + "1" * 32,
        ports=tuple(range(31001, 31011)),
        case=("constructed_started_v1", "41", str(tmp_path / "Power.log")),
        lane="lifecycle",
        edge_pre_line=17,
    )

    assert command[command.index("--lane") + 1] == "lifecycle"
    assert command[command.index("--edge-pre-line") + 1] == "17"
    case_index = command.index("--case")
    assert command[case_index + 1 : case_index + 3] == [
        "constructed_started_v1",
        "41",
    ]


def test_matrix_v6_report_requires_both_lifecycle_edges_before_host_start(
    tmp_path: Path,
) -> None:
    log = tmp_path / "Power.log"
    log.write_text("checkpoint\n", encoding="utf-8")
    cases = [(case_id, "1", str(log)) for case_id in matrix.supported_case_ids()]

    report, code = _run(
        SimpleNamespace(
            case=cases,
            lifecycle_edge=[
                ("constructed_started_v1", "1", "2", str(log)),
            ],
            single_case=False,
            neko_root="unused",
            neko_python="unused",
            storage_template="unused",
            role="unused",
        )
    )

    assert code == 1
    assert matrix.SCHEMA == "hearthstone_neko_isolated_answer_matrix_v6"
    assert report["schema"] == matrix.SCHEMA
    assert report["lifecycle_edges"] == []
    assert report["reason_code"] == "incomplete_lifecycle_edge_matrix"


def test_matrix_main_parses_both_lifecycle_edge_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}
    log = tmp_path / "Power.log"
    log.write_text("checkpoint\n", encoding="utf-8")

    def run(args: object) -> tuple[dict[str, object], int]:
        captured["lifecycle_edge"] = getattr(args, "lifecycle_edge")
        return {
            "schema": matrix.SCHEMA,
            "status": "ERROR",
            "lifecycle_edges": [],
            "cleanup": {},
        }, 1

    monkeypatch.setattr(matrix, "_run", run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "neko_answer_isolated_matrix.py",
            "--neko-root",
            str(tmp_path / "runtime"),
            "--neko-python",
            sys.executable,
            "--runtime-assets-dir",
            str(tmp_path / "assets"),
            "--storage-template",
            str(tmp_path / "storage"),
            "--role",
            "test-role",
            "--case",
            "constructed_round_v1",
            "1",
            str(log),
            "--lifecycle-edge",
            "constructed_started_v1",
            "17",
            "41",
            str(log),
            "--lifecycle-edge",
            "constructed_ended_v1",
            "83",
            "109",
            str(log),
        ],
    )

    assert matrix.main() == 1
    assert captured["lifecycle_edge"] == [
        ["constructed_started_v1", "17", "41", str(log)],
        ["constructed_ended_v1", "83", "109", str(log)],
    ]
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["schema"] == "hearthstone_neko_isolated_answer_matrix_v6"


def test_verified_callback_tools_requires_complete_registration_and_execution_proof() -> None:
    proof = {
        "tool_name": "hearthstone_current_turn",
        "proof_kind": "registered_callback_probe",
        "registration_source_verified": True,
        "remote_registration_verified": True,
        "callback_target_verified": True,
        "exact_once": True,
        "call_id_present": True,
        "status": "completed",
        "is_error": False,
        "output_contract": {"fact_verified": True},
    }
    cases = [{"lanes": {"query": {"probe": {"tool_callback_proofs": [proof]}}}}]

    assert _verified_callback_tools(cases) == ["hearthstone_current_turn"]

    for field in (
        "registration_source_verified",
        "remote_registration_verified",
        "callback_target_verified",
        "exact_once",
        "call_id_present",
    ):
        invalid = json.loads(json.dumps(cases))
        invalid[0]["lanes"]["query"]["probe"]["tool_callback_proofs"][0][field] = False
        assert _verified_callback_tools(invalid) == []

    invalid_result = json.loads(json.dumps(cases))
    invalid_result[0]["lanes"]["query"]["probe"]["tool_callback_proofs"][0]["is_error"] = True
    assert _verified_callback_tools(invalid_result) == []


def test_runtime_prepare_io_error_does_not_expose_private_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_path = str(tmp_path / "private-user-directory")
    log_path = tmp_path / "Power.log"
    log_path.write_text("checkpoint", encoding="utf-8")
    neko_root = tmp_path / "N.E.K.O"
    (neko_root / "app").mkdir(parents=True)
    assets = tmp_path / "assets"
    assets.mkdir()

    def fail_prepare_runtime(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise OSError(f"access denied: {private_path}")

    monkeypatch.setattr(
        "neko_answer_isolated_matrix._prepare_runtime",
        fail_prepare_runtime,
    )
    monkeypatch.setattr(
        "neko_answer_isolated_matrix._neko_source_identity",
        lambda _root: {"revision": "0" * 40, "clean": False},
    )

    report, code = _run(
        SimpleNamespace(
            case=[("constructed_round_v1", "1", str(log_path))],
            single_case=True,
            neko_root=str(neko_root),
            neko_python=sys.executable,
            storage_template=str(tmp_path / "storage"),
            runtime_assets_dir=str(assets),
            role="private-role",
        )
    )

    assert code == 1
    assert report["reason_code"] == "isolated_case_io_error"
    assert private_path not in repr(report)
    assert "private-role" not in repr(report)


def test_probe_timeout_stops_the_entire_process_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TimedOutProcess:
        def communicate(self, *, timeout: float) -> tuple[bytes, bytes]:
            raise subprocess.TimeoutExpired("private-command", timeout)

    process = TimedOutProcess()
    stopped: list[object] = []
    monkeypatch.setattr(
        "owned_process.stop_owned_process_tree",
        lambda candidate: stopped.append(candidate) or True,
    )

    with pytest.raises(subprocess.TimeoutExpired):
        _communicate_with_tree_cleanup(process, timeout=0.01)  # type: ignore[arg-type]

    assert stopped == [process]


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree regression")
def test_windows_owned_process_bootstrap_uses_base_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class BootstrapInput:
        def __init__(self) -> None:
            self.data = b""

        def write(self, value: bytes) -> None:
            self.data += value

        def flush(self) -> None:
            return None

        def close(self) -> None:
            return None

    class Process:
        def __init__(self) -> None:
            self.stdin: BootstrapInput | None = BootstrapInput()

    def popen(command: list[str], **kwargs: object) -> Process:
        process = Process()
        captured["command"] = command
        captured["kwargs"] = kwargs
        captured["input"] = process.stdin
        return process

    base_python = r"C:\Python311\python.exe"
    monkeypatch.setattr(owned_process.sys, "_base_executable", base_python)
    monkeypatch.setattr(owned_process.subprocess, "Popen", popen)
    monkeypatch.setattr(owned_process, "_assign_windows_kill_job", lambda _process: 123)

    owned_process.spawn_owned_process(["target.exe", "arg"])

    command = captured["command"]
    assert isinstance(command, list)
    assert command[0] == base_python
    assert command[-2:] == ["target.exe", "arg"]
    bootstrap_input = captured["input"]
    assert isinstance(bootstrap_input, BootstrapInput)
    assert bootstrap_input.data == b"1"


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree regression")
@pytest.mark.parametrize(
    ("final_state", "expected"),
    ((0x00000000, True), (0x00000102, False)),
    ids=("process_exited_during_final_wait", "process_still_running"),
)
def test_failed_terminate_defers_to_final_process_state(
    monkeypatch: pytest.MonkeyPatch,
    final_state: int,
    expected: bool,
) -> None:
    import ctypes

    class NativeCall:
        def __init__(self, function: Callable[..., int]) -> None:
            self._function = function
            self.argtypes: object = None
            self.restype: object = None

        def __call__(self, *args: Any) -> int:
            return self._function(*args)

    wait_results = iter((0x00000102, final_state))
    terminate_calls = 0

    def terminate_process(_handle: object, _exit_code: object) -> int:
        nonlocal terminate_calls
        terminate_calls += 1
        return 0

    kernel32 = SimpleNamespace(
        TerminateProcess=NativeCall(terminate_process),
        WaitForSingleObject=NativeCall(lambda *_args: next(wait_results)),
        CloseHandle=NativeCall(lambda *_args: 1),
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: kernel32)

    assert _terminate_windows_process_handles({1234: 5678}) is expected
    assert terminate_calls == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree regression")
def test_stop_process_tree_kills_child_after_parent_already_exited() -> None:
    parent_code = (
        "import subprocess,sys; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']); "
        "print(child.pid, flush=True)"
    )
    parent = _spawn_isolated_process(
        [sys.executable, "-c", parent_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        text=True,
    )
    assert parent.stdout is not None
    child_pid = int(parent.stdout.readline().strip())
    parent.wait(timeout=10.0)
    assert child_pid in _windows_process_snapshot()

    try:
        assert _stop_process_tree(parent) is True  # type: ignore[arg-type]
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and child_pid in _windows_process_snapshot():
            time.sleep(0.05)
        assert child_pid not in _windows_process_snapshot()
    finally:
        if child_pid in _windows_process_snapshot():
            subprocess.run(
                ["taskkill", "/PID", str(child_pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree regression")
def test_stop_process_tree_is_idempotent_for_the_same_owned_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _spawn_isolated_process(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )

    assert _stop_process_tree(process) is True
    monkeypatch.setattr(
        owned_process,
        "_terminate_windows_job",
        lambda _handle: pytest.fail("cached stop must not touch the closed job"),
    )
    monkeypatch.setattr(
        owned_process,
        "windows_process_snapshot",
        lambda: pytest.fail("cached stop must not rediscover a reused PID"),
    )

    assert _stop_process_tree(process) is True


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree regression")
def test_stop_process_tree_fails_closed_for_unowned_process() -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    try:
        assert _stop_process_tree(process) is False
        assert process.poll() is None
    finally:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        process.wait(timeout=10.0)


def test_cleanup_runtime_directory_retries_transient_windows_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    runtime = tmp_path / "neko-hearthstone-runtime-test"
    runtime.mkdir()
    real_rmtree = shutil.rmtree

    def flaky_rmtree(path: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("transient lock")
        real_rmtree(path)

    monkeypatch.setattr(owned_process.shutil, "rmtree", flaky_rmtree)
    monkeypatch.setattr(owned_process.time, "sleep", lambda _seconds: None)

    assert _cleanup_runtime_directory(runtime, timeout=1.0) is True
    assert attempts == 3
    assert not runtime.exists()


def _storage_template(root: Path) -> Path:
    template = root / "storage-template"
    config = template / "config"
    config.mkdir(parents=True)
    (config / "core_config.json").write_text("{}", encoding="utf-8")
    (config / "characters.json").write_text("{}", encoding="utf-8")
    return template


def test_isolated_case_retains_storage_when_process_tree_is_not_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = tmp_path / "neko-hearthstone-answer-case-retained"
    storage.mkdir()

    class Process:
        returncode = 0

    processes = [Process(), Process(), Process()]
    monkeypatch.setattr(matrix.tempfile, "mkdtemp", lambda **_kwargs: str(storage))
    monkeypatch.setattr(matrix, "_reserve_loopback_ports", lambda _count: tuple(range(10)))
    monkeypatch.setattr(matrix, "_wait_health", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(matrix, "_ports_released", lambda _ports: True)
    monkeypatch.setattr(matrix, "_spawn_isolated_process", lambda *_args, **_kwargs: processes.pop(0))
    monkeypatch.setattr(
        matrix,
        "_communicate_with_tree_cleanup",
        lambda *_args, **_kwargs: (b'{"status":"PASS"}\n', b""),
    )
    monkeypatch.setattr(matrix, "_stop_process_tree", lambda _process: False)

    _payload, cleanup = _run_isolated_case(
        project_root=tmp_path,
        neko_root=tmp_path,
        python_executable=Path(sys.executable),
        storage_template=_storage_template(tmp_path),
        role="test-role",
        case=("constructed_round_v1", "1", str(tmp_path / "Power.log")),
        lane="lifecycle",
    )

    assert cleanup["storage_removed"] is False
    assert storage.is_dir()
    shutil.rmtree(storage)


def test_isolated_case_rejects_nonzero_probe_even_with_pass_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = tmp_path / "neko-hearthstone-answer-case-nonzero"
    storage.mkdir()

    class Process:
        def __init__(self, returncode: int | None) -> None:
            self.returncode = returncode

    processes = [Process(None), Process(None), Process(7)]
    monkeypatch.setattr(matrix.tempfile, "mkdtemp", lambda **_kwargs: str(storage))
    monkeypatch.setattr(matrix, "_reserve_loopback_ports", lambda _count: tuple(range(10)))
    monkeypatch.setattr(matrix, "_wait_health", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(matrix, "_ports_released", lambda _ports: True)
    monkeypatch.setattr(matrix, "_spawn_isolated_process", lambda *_args, **_kwargs: processes.pop(0))
    monkeypatch.setattr(
        matrix,
        "_communicate_with_tree_cleanup",
        lambda *_args, **_kwargs: (b'{"status":"PASS"}\n', b"private-stderr"),
    )
    monkeypatch.setattr(matrix, "_stop_process_tree", lambda _process: True)

    payload, cleanup = _run_isolated_case(
        project_root=tmp_path,
        neko_root=tmp_path,
        python_executable=Path(sys.executable),
        storage_template=_storage_template(tmp_path),
        role="test-role",
        case=("constructed_round_v1", "1", str(tmp_path / "Power.log")),
        lane="query",
    )

    assert payload["status"] == "ERROR"
    assert payload["reason_code"] == "probe_process_failed"
    assert "private-stderr" not in repr(payload)
    assert cleanup["storage_removed"] is True
    assert not storage.exists()


def test_isolated_case_preserves_structured_nonzero_probe_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = tmp_path / "neko-hearthstone-answer-case-structured-failure"
    storage.mkdir()

    class Process:
        def __init__(self, returncode: int | None) -> None:
            self.returncode = returncode

    processes = [Process(None), Process(None), Process(1)]
    failure = {
        "schema": matrix.PROBE_SCHEMA,
        "status": "FAIL",
        "reason_code": "answer_timeout",
        "cases": [],
    }
    monkeypatch.setattr(matrix.tempfile, "mkdtemp", lambda **_kwargs: str(storage))
    monkeypatch.setattr(matrix, "_reserve_loopback_ports", lambda _count: tuple(range(10)))
    monkeypatch.setattr(matrix, "_wait_health", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(matrix, "_ports_released", lambda _ports: True)
    monkeypatch.setattr(matrix, "_spawn_isolated_process", lambda *_args, **_kwargs: processes.pop(0))
    monkeypatch.setattr(
        matrix,
        "_communicate_with_tree_cleanup",
        lambda *_args, **_kwargs: (json.dumps(failure).encode("utf-8") + b"\n", b"private-stderr"),
    )
    monkeypatch.setattr(matrix, "_stop_process_tree", lambda _process: True)

    payload, cleanup = _run_isolated_case(
        project_root=tmp_path,
        neko_root=tmp_path,
        python_executable=Path(sys.executable),
        storage_template=_storage_template(tmp_path),
        role="test-role",
        case=("constructed_round_v1", "1", str(tmp_path / "Power.log")),
        lane="query",
    )

    assert payload["status"] == "FAIL"
    assert payload["reason_code"] == "answer_timeout"
    assert "private-stderr" not in repr(payload)
    assert cleanup["storage_removed"] is True
    assert not storage.exists()


def test_standalone_matrix_is_nonzero_for_late_passive_invalidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = tmp_path / "Power.log"
    log.write_text("checkpoint\n", encoding="utf-8")
    neko_root = tmp_path / "N.E.K.O"
    (neko_root / "app").mkdir(parents=True)
    assets = tmp_path / "assets"
    assets.mkdir()
    runtime_container = tmp_path / "neko-hearthstone-runtime-test"

    def prepare_runtime(
        _source: Path,
        destination: Path,
        _assets: Path,
        *,
        revision: str,
    ) -> dict[str, object]:
        assert revision == "c" * 40
        destination.mkdir(parents=True)
        return {
            "source_export": {"revision": revision},
            "chat_assets": {},
        }

    def cleanup_runtime(path: Path) -> bool:
        shutil.rmtree(path)
        return True

    callback_proof = {
        "tool_name": "hearthstone_current_turn",
        "proof_kind": "registered_callback_probe",
        "registration_source_verified": True,
        "remote_registration_verified": True,
        "callback_target_verified": True,
        "exact_once": True,
        "call_id_present": True,
        "status": "completed",
        "is_error": False,
        "output_contract": {"fact_verified": True},
    }

    def run_case(**kwargs: object) -> tuple[dict[str, object], dict[str, bool]]:
        lane = kwargs["lane"]
        payload: dict[str, object] = {
            "schema": matrix.PROBE_SCHEMA,
            "status": "PASS",
            "cases": [{"status": "PASS"}],
            "tool_callback_proofs": [callback_proof] if lane == "query" else [],
        }
        if lane == "query":
            payload.update(
                status="FAIL",
                cases=[
                    {
                        "status": "FAIL",
                        "route": {
                            "passive_context": {
                                "status": "NOT_VERIFIED",
                                "no_later_invalidation": False,
                                "reason_codes": ["passive_context_invalidated"],
                            }
                        },
                    }
                ],
            )
        return payload, {
            "probe_stopped": True,
            "main_stopped": True,
            "memory_stopped": True,
            "ports_released": True,
            "storage_removed": True,
        }

    runtime_container.mkdir()
    monkeypatch.setattr(matrix.tempfile, "mkdtemp", lambda **_kwargs: str(runtime_container))
    monkeypatch.setattr(matrix, "_prepare_runtime", prepare_runtime)
    monkeypatch.setattr(matrix, "_cleanup_runtime_directory", cleanup_runtime)
    monkeypatch.setattr(matrix, "_run_isolated_case", run_case)
    monkeypatch.setattr(
        matrix,
        "_runtime_tree_fingerprint",
        lambda _root: {"sha256": "a" * 64, "file_count": 1},
    )
    monkeypatch.setattr(
        matrix,
        "_runtime_versions",
        lambda _root: {"app_version": "test", "sdk_version": "test"},
    )
    monkeypatch.setattr(
        matrix,
        "_python_runtime_identity",
        lambda _python: {"sha256": "b" * 64},
    )
    monkeypatch.setattr(
        matrix,
        "_neko_source_identity",
        lambda _root: {"revision": "c" * 40, "clean": True},
    )
    monkeypatch.setattr("release_evidence.source_fingerprint", lambda _root: "d" * 64)

    report, code = _run(
        SimpleNamespace(
            case=[("constructed_round_v1", "1", str(log))],
            lifecycle_edge=[],
            single_case=True,
            neko_root=str(neko_root),
            neko_python=sys.executable,
            storage_template=str(tmp_path / "storage"),
            runtime_assets_dir=str(assets),
            role="test-role",
        )
    )

    assert code == 1
    assert report["status"] == "FAIL"
    assert report["cases"][0]["lanes"]["query"]["status"] == "FAIL"
    passive = report["cases"][0]["lanes"]["query"]["probe"]["cases"][0][
        "route"
    ]["passive_context"]
    assert passive["no_later_invalidation"] is False
    assert passive["reason_codes"] == ["passive_context_invalidated"]
    assert not runtime_container.exists()
