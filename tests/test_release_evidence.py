from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path

import neko_answer_probe
import pytest
import release_evidence
from release_evidence import EvidenceError, validate_matrix

SOURCE_SHA256 = "a" * 64


def _matrix_cleanup() -> dict[str, bool]:
    return {key: True for key in release_evidence.MATRIX_CLEANUP_KEYS}


def _lane_cleanup() -> dict[str, bool]:
    return {key: True for key in release_evidence.LANE_CLEANUP_KEYS}


def _probe_cleanup() -> dict[str, bool]:
    return {key: True for key in release_evidence.PROBE_CLEANUP_KEYS}


def _python_identity() -> dict[str, object]:
    return {
        "implementation": "CPython",
        "version": "3.11.13",
        "cache_tag": "cpython-311",
        "executable_sha256": "c" * 64,
        "base_executable_sha256": "d" * 64,
        "environment_sha256": "e" * 64,
        "distribution_count": 10,
        "environment_file_count": 20,
    }


def _runtime() -> dict[str, object]:
    identity = _python_identity()
    asset_contract = release_evidence._expected_runtime_asset_contract()
    runtime_contract = asset_contract["runtime"]
    return {
        "schema": release_evidence.RUNTIME_SCHEMA,
        "isolated_mirror": True,
        "neko_source": {
            "expected_revision": release_evidence.EXPECTED_NEKO_REVISION,
            "revision_before": release_evidence.EXPECTED_NEKO_REVISION,
            "revision_after": release_evidence.EXPECTED_NEKO_REVISION,
            "clean_before": True,
            "clean_after": True,
            "stable": True,
            "release_compatible": True,
        },
        "source_export": {
            "method": runtime_contract["export_method"],
            "revision": release_evidence.EXPECTED_NEKO_REVISION,
            "root_tree": runtime_contract["root_git_tree"],
            "file_count": runtime_contract["source_file_count"],
        },
        "app_version": "0.9.0",
        "sdk_version": "0.1.0",
        "sha256_before": runtime_contract["final_sha256"],
        "sha256_after": runtime_contract["final_sha256"],
        "file_count_before": runtime_contract["final_file_count"],
        "file_count_after": runtime_contract["final_file_count"],
        "stable": True,
        "python": {
            "before": copy.deepcopy(identity),
            "after": copy.deepcopy(identity),
            "stable": True,
        },
        "chat_assets": asset_contract,
    }


def _checkpoint(case_id: str) -> dict[str, object]:
    expected = release_evidence.EXPECTED_CHECKPOINTS.get(case_id)
    if expected is None:
        expected = (case_id.split("_v", 1)[0], "constructed", 1)
    kind, mode, round_number = expected
    return {
        "kind": kind,
        "line": 20,
        "game_number": 1,
        "mode": mode,
        "round": round_number,
    }


def _readiness(*, callback_capability_verified: bool) -> dict[str, object]:
    return {
        "real_e2e_enabled": True,
        "health_verified": True,
        "isolation_verified": True,
        "role_available": True,
        "tools_registered": True,
        "official_plugin_service": True,
        "official_callback_observed": False,
        "all_expected_callbacks_observed": False,
        "callback_capability_verified": callback_capability_verified,
        "role_hash": "3" * 16,
    }


def _probe_privacy() -> dict[str, object]:
    return {
        **{flag: False for flag in release_evidence.PROBE_PRIVACY_FLAGS},
        "scan_completed": True,
        "source_counts": {
            key: 1 for key in release_evidence.PROBE_PRIVACY_COUNT_KEYS
        },
    }


def _matrix_privacy() -> dict[str, object]:
    return {
        **{flag: False for flag in release_evidence.MATRIX_PRIVACY_FLAGS},
        "scan_completed": True,
        "source_counts": {
            key: 1 for key in release_evidence.MATRIX_PRIVACY_COUNT_KEYS
        },
    }


def _query_isolation() -> dict[str, object]:
    return {
        "fresh_host_instance": True,
        "lifecycle_suppressed_before_session": True,
        "selector_sha256": release_evidence.LIFECYCLE_PROXY_SELECTOR_SHA256,
        "expected_suppressed_count": 1,
        "lifecycle_stage": "resumed",
        "suppressed_count": 1,
        "proxy_fatal_count": 0,
        "proxy_running_before_query": True,
        "quiet_after_capture": True,
    }


def _edge(case_id: str) -> dict[str, object]:
    ended = case_id == "constructed_ended_v1"
    return {
        "incremental_append": True,
        "pre_line": 10,
        "post_line": 20,
        "pre_bytes": 100,
        "post_bytes": 200,
        "appended_bytes": 100,
        "pre_submission_count": 1 if ended else 0,
        "pre_stage": "resumed" if ended else "",
        "pre_lifecycle": {},
        "pre_answer_observation_status": "FAIL",
        "pre_answer_observation_reason_codes": ["lifecycle_turn_incomplete"],
    }


def _callback_proof(tool_name: str) -> dict[str, object]:
    return {
        "tool_name": tool_name,
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


def _query_lane(case_id: str, tool_name: str) -> dict[str, object]:
    return {
        "status": "PASS",
        "cleanup": _lane_cleanup(),
        "probe": {
            "schema": release_evidence.PROBE_SCHEMA,
            "lane": "query",
            "status": "PASS",
            "readiness": _readiness(callback_capability_verified=True),
            "privacy": _probe_privacy(),
            "cleanup": _probe_cleanup(),
            "tool_callback_proofs": [_callback_proof(tool_name)],
            "cases": [
                {
                    "case_id": case_id,
                    "status": "PASS",
                    "answer_observation_status": "FAIL",
                    "answer_observation_reason_codes": ["shop_card_missing"],
                    "checkpoint": _checkpoint(case_id),
                    "query_isolation": _query_isolation(),
                    "route": {
                        "passive_context": {
                            "status": "VERIFIED",
                            "contract_sha256": release_evidence.PASSIVE_CONTEXT_CONTRACT_SHA256,
                            "observed_after_activation": True,
                            "observed_before_submit": True,
                            "envelope_verified": True,
                            "fact_verified": True,
                            "fact_sha256": "b" * 64,
                            "fact_count": 10,
                            "match_id": 1,
                            "mode": _checkpoint(case_id)["mode"],
                            "round": _checkpoint(case_id)["round"],
                            "segment": "core",
                            "coalesce_key_sha256": "5" * 64,
                            "semantic_fingerprint": "6" * 16,
                            "forwarded_sequence": 1,
                            "observation_count": 1,
                            "no_later_invalidation": True,
                            "reason_codes": [],
                        }
                    },
                }
            ],
        },
    }


def _lifecycle_lane(case_id: str, stage: str) -> dict[str, object]:
    return {
        "status": "PASS",
        "cleanup": _lane_cleanup(),
        "probe": {
            "schema": release_evidence.PROBE_SCHEMA,
            "lane": "lifecycle",
            "status": "PASS",
            "readiness": _readiness(callback_capability_verified=False),
            "privacy": _probe_privacy(),
            "cleanup": _probe_cleanup(),
            "cases": [
                {
                    "case_id": case_id,
                    "status": "PASS",
                    "answer_observation_status": "FAIL",
                    "answer_observation_reason_codes": ["lifecycle_turn_incomplete"],
                    "checkpoint": _checkpoint(case_id),
                    "lifecycle": {
                        "submission_count": 1,
                        "lifecycle_stage": stage,
                        "visible_turn_count": 0,
                        "possibly_batched": False,
                    },
                    **({"edge": _edge(case_id)} if case_id in release_evidence.EXPECTED_LIFECYCLE_EDGES else {}),
                }
            ],
        },
    }


def _matrix() -> dict[str, object]:
    cases = []
    for case_id, tool_name in release_evidence.EXPECTED_CASE_TO_TOOL.items():
        cases.append(
            {
                "case_id": case_id,
                "status": "PASS",
                "lanes": {
                    "query": _query_lane(case_id, tool_name),
                    "lifecycle": _lifecycle_lane(case_id, "resumed"),
                },
            }
        )
    edges = [
        {
            "case_id": case_id,
            "status": "PASS",
            "cleanup": _lane_cleanup(),
            "probe": _lifecycle_lane(case_id, stage)["probe"],
        }
        for case_id, stage in release_evidence.EXPECTED_LIFECYCLE_EDGES.items()
    ]
    return {
        "schema": release_evidence.MATRIX_SCHEMA,
        "status": "PASS",
        "source": {
            "stable": True,
            "sha256_before": SOURCE_SHA256,
            "sha256_after": SOURCE_SHA256,
        },
        "runtime": _runtime(),
        "cleanup": _matrix_cleanup(),
        "tool_callback_coverage": {"all_verified": True},
        "cases": cases,
        "lifecycle_edges": edges,
        "privacy": _matrix_privacy(),
    }


def test_model_answer_failures_do_not_block_deterministic_chain_evidence() -> None:
    validate_matrix(_matrix(), source_sha256=SOURCE_SHA256)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda matrix: matrix["cases"][0]["lanes"]["query"]["probe"][
            "tool_callback_proofs"
        ][0].update(exact_once=False),
        lambda matrix: matrix["cases"][0]["lanes"]["query"]["probe"]["cases"][
            0
        ]["route"]["passive_context"].update(status="NOT_VERIFIED"),
        lambda matrix: matrix["cases"][0]["lanes"]["lifecycle"]["probe"][
            "cases"
        ][0]["lifecycle"].update(submission_count=0),
        lambda matrix: matrix["cleanup"].update(all_processes_stopped=False),
    ],
    ids=("callback", "passive", "lifecycle", "cleanup"),
)
def test_deterministic_chain_failures_remain_blocking(
    mutate: Callable[[dict[str, object]], None],
) -> None:
    matrix = _matrix()
    mutate(matrix)

    with pytest.raises(EvidenceError):
        validate_matrix(matrix, source_sha256=SOURCE_SHA256)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda matrix: matrix.update(cleanup={"fabricated": True}),
        lambda matrix: matrix["cases"][0]["lanes"]["query"].update(
            cleanup={"fabricated": True}
        ),
        lambda matrix: matrix["cases"][0]["lanes"]["query"]["probe"].update(
            cleanup={"fabricated": True}
        ),
        lambda matrix: matrix["lifecycle_edges"][0].update(cleanup={"fabricated": True}),
        lambda matrix: matrix.update(
            runtime={
                "isolated_mirror": True,
                "stable": True,
                "python": {"stable": True},
            }
        ),
        lambda matrix: matrix["runtime"].update(sha256_after="0" * 64),
        lambda matrix: matrix["runtime"]["neko_source"].update(
            revision_before="0" * 40
        ),
        lambda matrix: matrix["runtime"]["neko_source"].update(
            clean_after=False
        ),
        lambda matrix: matrix["runtime"]["python"]["after"].update(
            environment_sha256="0" * 64
        ),
        lambda matrix: matrix["runtime"]["source_export"].update(
            method="worktree_copy"
        ),
        lambda matrix: matrix["runtime"]["source_export"].update(
            root_tree="0" * 40
        ),
        lambda matrix: matrix["runtime"]["chat_assets"]["files"].pop(
            "neko-chat-window.css"
        ),
        lambda matrix: matrix["runtime"]["chat_assets"].update(
            manifest_sha256="0" * 64
        ),
        lambda matrix: matrix["cases"][0]["lanes"]["query"]["probe"]["cases"][0].pop(
            "checkpoint"
        ),
        lambda matrix: matrix["cases"][0]["lanes"]["query"]["probe"]["cases"][0].pop(
            "query_isolation"
        ),
        lambda matrix: matrix["lifecycle_edges"][0]["probe"]["cases"][0]["edge"].update(
            incremental_append=False
        ),
        lambda matrix: matrix["privacy"].update(absolute_path_emitted=True),
        lambda matrix: matrix.update(unvalidated_note="C:/Users/Alice/private/Power.log"),
        lambda matrix: matrix.update({"C:/Users/Alice/private/Power.log": True}),
        lambda matrix: matrix.update(unvalidated_note="/Power.log"),
        lambda matrix: matrix["cases"][0]["lanes"]["query"]["probe"]["cases"][0][
            "query_isolation"
        ].update(
            expected_suppressed_count=True,
            suppressed_count=True,
            proxy_fatal_count=False,
        ),
        lambda matrix: matrix["cases"][0]["lanes"]["lifecycle"]["probe"]["cases"][0][
            "lifecycle"
        ].update(submission_count=True),
        lambda matrix: matrix["lifecycle_edges"][1]["probe"]["cases"][0]["edge"].update(
            pre_submission_count=True
        ),
        lambda matrix: matrix["cases"][0]["lanes"]["query"]["probe"]["cases"][0][
            "query_isolation"
        ].update(selector_sha256="0" * 64),
        lambda matrix: matrix["cases"][0]["lanes"]["query"]["probe"]["cases"][0][
            "route"
        ]["passive_context"].pop("contract_sha256"),
        lambda matrix: matrix["cases"][0]["lanes"]["query"]["probe"]["cases"][0][
            "route"
        ]["passive_context"].update(mode="battlegrounds"),
    ],
    ids=(
        "matrix-cleanup-fields",
        "lane-cleanup-fields",
        "probe-cleanup-fields",
        "edge-cleanup-fields",
        "runtime-fields",
        "runtime-fingerprint",
        "neko-revision",
        "neko-source-dirty",
        "python-identity",
        "source-export-method",
        "source-export-tree",
        "chat-asset-files",
        "chat-asset-manifest",
        "checkpoint",
        "query-isolation",
        "lifecycle-edge",
        "privacy-status",
        "privacy-content",
        "privacy-key",
        "privacy-posix-root-path",
        "query-isolation-bool-counts",
        "lifecycle-bool-count",
        "edge-bool-count",
        "selector-contract",
        "passive-contract",
        "passive-checkpoint",
    ),
)
def test_stripped_deterministic_evidence_is_rejected(
    mutate: Callable[[dict[str, object]], None],
) -> None:
    matrix = _matrix()
    mutate(matrix)

    with pytest.raises(EvidenceError):
        validate_matrix(matrix, source_sha256=SOURCE_SHA256)


def test_release_evidence_writer_binds_version_and_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "plugin.toml").write_text(
        '[plugin]\nid = "hearthstone_companion"\nversion = "0.4.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\nversion = "0.4.0"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(release_evidence, "source_fingerprint", lambda _root: SOURCE_SHA256)
    output = tmp_path / ".github" / "e2e-evidence" / "v0.4.0.json"

    written = release_evidence.write_release_evidence(tmp_path, _matrix(), output)
    loaded = release_evidence.validate_release_evidence(
        tmp_path,
        output,
        tag="v0.4.0",
    )

    assert loaded == written
    assert loaded["source_sha256"] == SOURCE_SHA256
    assert loaded["neko_revision"] == release_evidence.EXPECTED_NEKO_REVISION
    assert loaded["matrix"]["cases"][0]["lanes"]["query"]["probe"]["cases"][0][
        "answer_observation_status"
    ] == "FAIL"


def test_source_fingerprint_is_path_bound_and_normalizes_newlines(tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_bytes(b"value\r\n")
    second.write_bytes(b"value\n")

    one = release_evidence.source_fingerprint(tmp_path, paths=["first.txt"])
    first.write_bytes(b"value\n")
    normalized = release_evidence.source_fingerprint(tmp_path, paths=["first.txt"])
    other_path = release_evidence.source_fingerprint(tmp_path, paths=["second.txt"])

    assert one == normalized
    assert other_path != normalized


def test_validate_matrix_does_not_mutate_report() -> None:
    matrix = _matrix()
    before = copy.deepcopy(matrix)

    validate_matrix(matrix, source_sha256=SOURCE_SHA256)

    assert matrix == before


def test_release_constants_match_probe_protocol() -> None:
    assert (
        release_evidence.LIFECYCLE_PROXY_SELECTOR_SHA256
        == neko_answer_probe.LIFECYCLE_PROXY_SELECTOR_SHA256
    )
    assert (
        release_evidence.PASSIVE_CONTEXT_CONTRACT_SHA256
        == neko_answer_probe.PASSIVE_CONTEXT_CONTRACT_SHA256
    )


def test_release_workflow_uses_the_evidence_pinned_neko_revision() -> None:
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"
    ).read_text(encoding="utf-8")
    revision = release_evidence.EXPECTED_NEKO_REVISION

    assert f"ref: {revision}" in workflow
    assert f"plugin-market-release.yml@{revision}" in workflow
    assert f"neko-ref: {revision}" in workflow
