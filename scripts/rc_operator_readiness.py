"""Offline material completeness, never authority to execute a staging drill."""

import argparse
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field
from rc_acceptance import Digest, Frozen
from rc_causal import CausalChallenge
from rc_evidence_custody import bind_registry, decode, load, read_file

from agentflow.application.ports.model_request import canonical_json


class OperatorMaterials(Frozen):
    schema_version: Literal[1] = 1
    environment_id: UUID
    scope_approval_digest: Digest
    operator_identity: Digest
    independent_approver_identity: Digest
    host_inventory_digest: Digest
    worker_hosts: tuple[Digest, ...] = Field(min_length=2, max_length=3)
    deployment_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    trust_sha256: Digest
    collector_sha256: Digest
    private_plan_sha256: Digest
    preflight_sha256: Digest
    database_oid: int = Field(strict=True, gt=0)
    database_marker: UUID
    tls_preflight_digest: Digest
    alembic_revision: Literal["0008"]
    synthetic_task_limit: int = Field(strict=True, ge=1, le=20)
    fault_window_start: AwareDatetime
    fault_window_end: AwareDatetime
    stop_conditions_digest: Digest
    unknown_recovery_runbook_digest: Digest
    lease_recovery_runbook_digest: Digest
    private_retention_policy_digest: Digest
    aj_runbook_digest: Digest


def readiness(
    store, manifest_digest, registry, materials_path, trust_pin, collector_pin
):
    try:
        snapshot = load(store, manifest_digest)
        manifest = snapshot.manifest
        challenge = CausalChallenge.model_validate(
            decode(snapshot.data["challenge.json"])
        )
        materials = OperatorMaterials.model_validate(
            decode(read_file(materials_path, 16000))
        )
        config = decode(snapshot.data["config.json"])
        records = {r.path: r.sha256 for r in manifest.files}
        workers = {h["host_digest"] for h in config["hosts"] if h["role"] == "worker"}
        plan = decode(snapshot.data["plan.json"])
        if (
            bind_registry(snapshot.store, challenge, registry)
            != manifest.registry_binding_digest
            or materials.environment_id != challenge.environment_id
            or materials.operator_identity == materials.independent_approver_identity
            or len(set(materials.worker_hosts)) != len(materials.worker_hosts)
            or set(materials.worker_hosts) != workers
            or materials.deployment_sha != challenge.deployment_sha
            or materials.trust_sha256 != trust_pin
            or manifest.trust_sha256 != trust_pin
            or materials.collector_sha256 != collector_pin
            or manifest.collector_sha256 != collector_pin
            or materials.private_plan_sha256 != records["plan.json"]
            or materials.preflight_sha256 != records["v1.json"]
            or materials.database_oid != config["database_oid"]
            or materials.database_marker != challenge.environment_id
            or len(plan["runs"]) > materials.synthetic_task_limit
            or not challenge.issued_at
            <= materials.fault_window_start
            < materials.fault_window_end
            <= challenge.expires_at
            or (
                materials.fault_window_end - materials.fault_window_start
            ).total_seconds()
            > 600
        ):
            raise ValueError
        snapshot.verify()
        return {
            "schema_version": 1,
            "status": "GO_FOR_OPERATOR_REVIEW",
            "execution_authorized": False,
            "manifest_digest": manifest_digest,
            "physical_sources": "NOT_VERIFIED",
            "production_release": "NOT_VERIFIED",
        }
    except Exception:
        return {
            "schema_version": 1,
            "status": "NOT_READY",
            "execution_authorized": False,
            "error": "OperatorMaterialsRejected",
            "production_release": "NOT_VERIFIED",
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "store",
        "manifest-sha256",
        "state-dir",
        "materials",
        "trust-sha256",
        "collector-sha256",
    ):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    result = readiness(
        args.store,
        args.manifest_sha256,
        args.state_dir,
        args.materials,
        args.trust_sha256,
        args.collector_sha256,
    )
    print(canonical_json(result))
    return 0 if result["status"] == "GO_FOR_OPERATOR_REVIEW" else 1


if __name__ == "__main__":
    raise SystemExit(main())
