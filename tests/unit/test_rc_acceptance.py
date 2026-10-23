"""Synthetic keys and receipts only; these tests never certify real hosts."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_rc_tools import script


@pytest.fixture
def acceptance(monkeypatch):
    return script(monkeypatch, "rc_acceptance")


@pytest.fixture
def ceremony(acceptance):
    m = acceptance
    now = datetime.now(timezone.utc)
    scope = dict(
        environment_id="11111111-2222-4333-8444-555555555555",
        deployment_sha="a" * 40,
        application_version="1.0.0",
        alembic_revision="0008",
        database_identity_digest="b" * 64,
        staging_batch_digest="c" * 64,
        v1_report_digest="d" * 64,
    )
    challenge = m.AcceptanceChallenge(
        **scope,
        execution_nonce="e" * 64,
        issued_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    keys, signers = {}, []
    for role in (
        "database",
        "build",
        "infrastructure",
        "reviewer",
        "fault",
        "host",
        "host",
    ):
        key = Ed25519PrivateKey.generate()
        public = key.public_key().public_bytes_raw().hex()
        identity = m.request_digest(public)
        keys[identity] = key
        signers.append(
            m.TrustedSigner(
                key_id=identity,
                public_key=public,
                role=role,
                host_digest=str(len(signers)) * 64 if role == "host" else None,
                not_before=now - timedelta(days=1),
                not_after=now + timedelta(days=1),
            )
        )
    trust = m.AcceptanceTrust(version=1, signers=tuple(signers))

    def receipt(payload, role="database", index=0):
        signer = [s for s in signers if s.role == role][index]
        evidence = m.AcceptanceEvidence(
            challenge=challenge,
            attestation_identity=signer.key_id,
            started_at=now,
            completed_at=now,
            payload=payload,
        )
        return m.SignedEvidence(
            evidence=evidence,
            evidence_digest=m.request_digest(evidence.model_dump(mode="json")),
            signature=keys[signer.key_id].sign(m.signing_bytes(evidence)).hex(),
        )

    return m, now, challenge, trust, receipt


def test_missing_sources_never_pass(ceremony):
    m, now, challenge, trust, _ = ceremony
    report = m.verify_acceptance(challenge, trust, (), now=now)
    assert report.schema_version == 2
    assert {case.status for case in report.cases} == {"NOT_VERIFIED"}
    assert (
        report.live_database_status
        == report.provider_acceptance
        == report.token_conformance
        == "NOT_VERIFIED"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("environment_id", "22222222-2222-4333-8444-555555555555"),
        ("deployment_sha", "f" * 40),
        ("database_identity_digest", "f" * 64),
        ("staging_batch_digest", "f" * 64),
    ],
)
def test_cross_scope_receipt_is_rejected(ceremony, field, value):
    m, now, challenge, trust, receipt = ceremony
    signed = receipt(
        m.DatabaseEvidence(
            case_id="A", phase="before", snapshot=m.DatabaseSnapshot(observed_at=now)
        )
    )
    other = m.AcceptanceChallenge.model_validate(
        {**challenge.model_dump(), field: value}
    )
    with pytest.raises(ValueError, match="EvidenceScopeMismatch"):
        m.verify_acceptance(other, trust, (signed,), now=now)


def test_expired_tampered_unregistered_and_duplicate_receipts(ceremony):
    m, now, challenge, trust, receipt = ceremony
    signed = receipt(
        m.DatabaseEvidence(
            case_id="A", phase="before", snapshot=m.DatabaseSnapshot(observed_at=now)
        )
    )
    with pytest.raises(ValueError, match="EvidenceExpired"):
        m.verify_acceptance(challenge, trust, (signed,), now=now + timedelta(hours=1))
    raw = signed.model_dump(mode="json")
    raw["signature"] = "0" * 128
    with pytest.raises(ValueError, match="EvidenceSignatureRejected"):
        m.verify_acceptance(
            challenge, trust, (m.SignedEvidence.model_validate(raw),), now=now
        )
    empty = m.AcceptanceTrust(version=2, signers=())
    with pytest.raises(ValueError, match="UntrustedEvidenceSigner"):
        m.verify_acceptance(challenge, empty, (signed,), now=now)
    with pytest.raises(ValueError, match="EvidenceReplay"):
        m.verify_acceptance(challenge, trust, (signed, signed), now=now)
    raw = signed.model_dump(mode="json")
    raw["evidence"]["payload"]["phase"] = "after"
    with pytest.raises(ValueError, match="EvidenceDigestMismatch"):
        m.verify_acceptance(
            challenge, trust, (m.SignedEvidence.model_validate(raw),), now=now
        )


def test_challenge_is_single_use_across_invocations(ceremony, tmp_path):
    m, _, challenge, _, _ = ceremony
    state = tmp_path / "private"
    m.issue_challenge(state, challenge)
    m.consume_challenge(state, challenge)
    with pytest.raises(ValueError, match="ChallengeReplay"):
        m.consume_challenge(state, challenge)


def test_concurrent_challenge_consumption_has_one_winner(ceremony, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    m, _, challenge, _, _ = ceremony
    state = tmp_path / "private"
    m.issue_challenge(state, challenge)

    def consume():
        try:
            m.consume_challenge(state, challenge)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: consume(), range(2))) == [False, True]


def test_role_confusion_and_expired_signer_are_rejected(ceremony):
    m, now, challenge, trust, sign = ceremony
    host = next(s for s in trust.signers if s.role == "host")
    receipt = sign(
        m.HostEvidence(case_id="A", host_digest=host.host_digest, operations=()),
        "database",
    )
    with pytest.raises(ValueError, match="UntrustedEvidenceSigner"):
        m.verify_acceptance(challenge, trust, (receipt,), now=now)
    receipt = sign(
        m.DatabaseEvidence(
            case_id="A", phase="before", snapshot=m.DatabaseSnapshot(observed_at=now)
        )
    )
    raw = trust.model_dump()
    for signer in raw["signers"]:
        signer["not_after"] = now - timedelta(seconds=1)
    expired = m.AcceptanceTrust.model_validate(raw)
    with pytest.raises(ValueError, match="EvidenceExpired"):
        m.verify_acceptance(challenge, expired, (receipt,), now=now)


def test_input_json_cannot_hide_duplicate_fields_or_nonfinite_values(
    monkeypatch, tmp_path
):
    m = script(monkeypatch, "verify_rc_acceptance")
    path = tmp_path / "data.json"
    for document in (
        '{"source":"host","source":"database"}',
        '{"time":NaN}',
        '{"time":Infinity}',
    ):
        path.write_text(document)
        with pytest.raises(ValueError):
            m.bounded_json(path, 100)
    path.write_text(" " * 101)
    with pytest.raises(ValueError, match="EvidenceBoundsExceeded"):
        m.bounded_json(path, 100)


def test_equal_revision_changed_snapshot_and_deleted_permit_fail(acceptance):
    m = acceptance
    now = datetime.now(timezone.utc)
    step = m.StepFact(
        ref="1" * 64,
        run_ref="2" * 64,
        run_status="running",
        status="ready",
        revision=3,
        snapshot_digest="3" * 64,
        agent_identity="4" * 64,
    )
    provider = m.ProviderFact(
        ref="5" * 64,
        max_concurrent=2,
        max_requests=2,
        window_seconds=10.0,
        cooldown_until=0.0,
    )
    permit = m.PermitFact(
        ref="6" * 64,
        attempt_ref="7" * 64,
        provider_ref=provider.ref,
        status="released",
        created_at=now.timestamp() - 5,
        expires_at=now.timestamp(),
        released_at=now.timestamp() - 1,
    )
    before = m.DatabaseSnapshot(
        observed_at=now, steps=(step,), permits=(permit,), providers=(provider,)
    )
    changed = m.StepFact.model_validate(
        {**step.model_dump(), "snapshot_digest": "8" * 64}
    )
    after = m.DatabaseSnapshot(observed_at=now, steps=(changed,), providers=(provider,))
    assert "PersistentHistoryChanged" in m.transition_failures(before, after)


def test_witnesses_require_the_same_persistent_target_and_a_new_reservation(acceptance):
    m = acceptance
    now = datetime.now(timezone.utc)
    step = m.StepFact(
        ref="1" * 64,
        run_ref="2" * 64,
        run_status="running",
        status="running",
        lease_digest="3" * 64,
        lease_until=now.timestamp() + 10,
    )
    other = m.StepFact.model_validate({**step.model_dump(), "ref": "4" * 64})
    changed = m.StepFact.model_validate({**step.model_dump(), "lease_digest": "5" * 64})
    before = m.DatabaseSnapshot(observed_at=now, steps=(step, other))
    after = m.DatabaseSnapshot(observed_at=now, steps=(changed, other))
    rejected_other = m.Operation(
        kind="stale_commit",
        target=other.ref,
        outcome="rejected",
        error_code="LostLease",
        at=now.timestamp(),
    )
    assert not m.scenario_witness("C", before, after, (rejected_other,))
    claim = m.Operation(
        kind="claim",
        target=step.ref,
        outcome="accepted",
        at=now.timestamp(),
        lease_until=now.timestamp() + 10,
    )
    assert not m.scenario_witness("A", before, before, (claim, claim))
    provider = m.ProviderFact(
        ref="6" * 64,
        max_concurrent=2,
        max_requests=2,
        window_seconds=10.0,
        cooldown_until=0.0,
    )
    permit = m.PermitFact(
        ref="7" * 64,
        attempt_ref="8" * 64,
        provider_ref=provider.ref,
        status="active",
        created_at=now.timestamp() - 5,
        expires_at=now.timestamp() + 10,
    )
    unchanged = m.DatabaseSnapshot(
        observed_at=now, permits=(permit,), providers=(provider,)
    )
    reserve = m.Operation(
        kind="model_reserve", target=step.ref, outcome="accepted", at=now.timestamp()
    )
    assert not m.scenario_witness("F", unchanged, unchanged, (reserve, reserve))


@pytest.mark.parametrize("version", (2, 3))
def test_cli_issue_verify_and_replay_are_offline_and_redacted(
    monkeypatch, tmp_path, capsys, version
):
    import hashlib

    from test_rc_staging import configuration, facts

    module = script(monkeypatch, "verify_rc_acceptance")
    import rc_staging

    config = configuration(rc_staging)
    original = rc_staging.report(config, facts=facts(rc_staging), errors=())
    plan = {
        "environment": str(config.environment_id),
        "batch_id": "11111111-2222-4333-8444-555555555555",
        "runs": [{"id": "22222222-2222-4333-8444-555555555555", "scenario": "final"}],
    }
    for name, value in (
        ("config", config.model_dump(mode="json")),
        ("v1", original.model_dump(mode="json")),
        ("plan", plan),
    ):
        (tmp_path / f"{name}.json").write_text(json.dumps(value), encoding="utf-8")
    common = [
        "--config",
        str(tmp_path / "config.json"),
        "--v1-report",
        str(tmp_path / "v1.json"),
        "--plan",
        str(tmp_path / "plan.json"),
        "--state-dir",
        str(tmp_path / "private"),
    ]
    monkeypatch.setenv("AGENTFLOW_RC_DATABASE_URL", "PRIVATE_DATABASE_CREDENTIAL")
    monkeypatch.setattr(
        rc_staging, "connect", lambda *a, **kw: pytest.fail("OfflineVerifierConnected")
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "verify_rc_acceptance.py",
            "issue",
            *common,
            "--database-identity-digest",
            "1" * 64,
        ],
    )
    assert module.main(evidence_version=version) == 0
    challenge = capsys.readouterr().out
    challenge_path = tmp_path / "challenge.json"
    challenge_path.write_text(challenge)
    trust = tmp_path / "trust.json"
    trust.write_text('{"version":1,"signers":[]}')
    command = [
        "verify_rc_acceptance.py",
        "verify",
        *common,
        "--challenge",
        str(challenge_path),
        "--trust",
        str(trust),
        "--trust-sha256",
        hashlib.sha256(trust.read_bytes()).hexdigest(),
    ]
    monkeypatch.setattr("sys.argv", command)
    assert module.main(evidence_version=version) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["schema_version"] == version
    assert {c["status"] for c in json.loads(output)["cases"]} == {"NOT_VERIFIED"}
    assert "PRIVATE" not in output and plan["runs"][0]["id"] not in output
    assert module.main(evidence_version=version) == 2
    assert (
        json.loads(capsys.readouterr().out)["error"] == "AcceptanceVerificationRejected"
    )


def test_signature_and_inventory_are_not_physical_host_proof(ceremony):
    m, now, challenge, trust, receipt = ceremony
    host = [s for s in trust.signers if s.role == "host"][0]
    signed = receipt(
        m.HostEvidence(case_id="A", host_digest=host.host_digest, operations=()), "host"
    )
    report = m.verify_acceptance(challenge, trust, (signed,), now=now)
    assert report.cases[0].status == "NOT_VERIFIED"
    assert "InfrastructureEvidenceMissing" in report.cases[0].limitations
    assert "FaultPhaseMissing" in report.cases[0].limitations


@pytest.mark.parametrize(
    "kind,code",
    [
        ("stale_commit", "StaleCommitAccepted"),
        ("unknown_dispatch", "UnknownRedispatched"),
        ("cancelled_commit", "CancelledLateCommit"),
    ],
)
def test_accepted_unsafe_operation_is_a_failure_even_with_missing_evidence(
    ceremony, kind, code
):
    m, now, challenge, trust, receipt = ceremony
    host = [s for s in trust.signers if s.role == "host"][0]
    operation = m.Operation(
        kind=kind, target="1" * 64, outcome="accepted", at=now.timestamp()
    )
    signed = receipt(
        m.HostEvidence(
            case_id="C", host_digest=host.host_digest, operations=(operation,)
        ),
        "host",
    )
    report = m.verify_acceptance(challenge, trust, (signed,), now=now)
    case = next(c for c in report.cases if c.case_id == "C")
    assert case.status == "FAIL" and code in case.observed_invariants


@pytest.mark.parametrize(
    "field,code", [("permits", "PermitDuplicate"), ("artifacts", "ArtifactDuplicate")]
)
def test_duplicate_persistent_facts_fail(ceremony, field, code):
    m, now, challenge, trust, receipt = ceremony
    step = m.StepFact(
        ref="1" * 64, run_ref="2" * 64, run_status="running", status="ready"
    )
    if field == "artifacts":
        value = m.ArtifactFact(step_ref=step.ref, digest="3" * 64)
    else:
        value = m.PermitFact(
            ref="3" * 64,
            attempt_ref="4" * 64,
            provider_ref="5" * 64,
            status="active",
            created_at=now.timestamp(),
            expires_at=now.timestamp() + 30,
        )
    snapshot = m.DatabaseSnapshot(
        observed_at=now, steps=(step,), **{field: (value, value)}
    )
    signed = receipt(m.DatabaseEvidence(case_id="F", phase="after", snapshot=snapshot))
    report = m.verify_acceptance(challenge, trust, (signed,), now=now)
    assert report.cases[5].status == "FAIL"
    assert code in report.cases[5].observed_invariants


def test_waiting_is_valid_but_inflight_waiting_is_corruption(acceptance):
    m = acceptance
    step = m.StepFact(
        ref="1" * 64,
        run_ref="2" * 64,
        run_status="running",
        status="waiting",
        agent_status="waiting_reconciliation",
    )
    action = m.ActionFact(
        ref="3" * 64, step_ref=step.ref, status="unknown", identity_digest="4" * 64
    )
    snapshot = m.DatabaseSnapshot(
        observed_at=datetime.now(timezone.utc), steps=(step,), actions=(action,)
    )
    assert m.snapshot_failures(snapshot) == ()
    bad = m.DatabaseSnapshot.model_validate(
        {
            **snapshot.model_dump(),
            "actions": [{**action.model_dump(), "status": "inflight"}],
        }
    )
    assert "WaitingInflight" in m.snapshot_failures(bad)


def test_receipts_reject_unbounded_and_sensitive_fields(acceptance):
    m = acceptance
    with pytest.raises(ValueError):
        m.Operation(
            kind="stale_commit",
            target="1" * 64,
            outcome="rejected",
            at=0,
            token="PRIVATE_TOKEN",
        )
    with pytest.raises(ValueError):
        m.DatabaseSnapshot(
            observed_at=datetime.now(timezone.utc), prompt="PRIVATE_PROMPT"
        )


def test_cli_errors_are_fixed_codes_and_offline_by_default(
    monkeypatch, tmp_path, capsys
):
    m = script(monkeypatch, "verify_rc_acceptance")
    path = tmp_path / "untrusted.json"
    path.write_text(json.dumps({"password": "PRIVATE_KEY", "prompt": "PRIVATE_PROMPT"}))
    monkeypatch.setattr(
        "sys.argv", ["verify_rc_acceptance.py", "verify", "--challenge", str(path)]
    )
    assert m.main() == 2
    output = capsys.readouterr()
    assert "PRIVATE" not in output.out + output.err
    assert json.loads(output.out)["error"] == "AcceptanceVerificationRejected"


def test_complete_independently_signed_case_and_missing_fault(ceremony):
    m, now, challenge, trust, sign = ceremony
    host_ids = [s.host_digest for s in trust.signers if s.role == "host"]
    step = m.StepFact(
        ref="1" * 64,
        run_ref="2" * 64,
        run_status="running",
        status="running",
        lease_digest="3" * 64,
        lease_until=now.timestamp() + 10,
    )
    snapshot = m.DatabaseSnapshot(
        observed_at=now, database_backend="postgresql", steps=(step,)
    )
    identity = m.DeploymentIdentity(
        git_commit_sha=challenge.deployment_sha,
        application_version="1.0.0",
        runtime_digest="1" * 64,
        admission_digest="2" * 64,
        usage_mapping_digest="3" * 64,
        tool_digest="4" * 64,
    )
    receipts = [
        sign(m.BuildEvidence(identity=identity), "build"),
        sign(
            m.InfrastructureEvidence(
                inventory_digest="1" * 64,
                host_failure_domains=((host_ids[0], "1" * 64), (host_ids[1], "2" * 64)),
            ),
            "infrastructure",
        ),
        sign(
            m.DatabaseEvidence(
                case_id="A",
                phase="before",
                snapshot=m.DatabaseSnapshot(
                    observed_at=now,
                    database_backend="postgresql",
                    steps=(
                        m.StepFact.model_validate(
                            {
                                **step.model_dump(),
                                "status": "ready",
                                "lease_digest": None,
                                "lease_until": None,
                            }
                        ),
                    ),
                ),
            )
        ),
        sign(m.DatabaseEvidence(case_id="A", phase="after", snapshot=snapshot)),
        sign(
            m.FaultEvidence(
                case_id="A", phase="lease_competition", protected_record_digest="5" * 64
            ),
            "fault",
        ),
    ]
    for index, host in enumerate(host_ids):
        operation = m.Operation(
            kind="claim",
            target=step.ref,
            outcome="accepted" if index == 0 else "unavailable",
            at=now.timestamp(),
            lease_until=now.timestamp() + 10 if index == 0 else None,
        )
        receipts.append(
            sign(
                m.HostEvidence(case_id="A", host_digest=host, operations=(operation,)),
                "host",
                index,
            )
        )
    review = sign(
        m.ReviewEvidence(
            case_id="A",
            reviewed_evidence_digest=m.evidence_set_digest(receipts),
            review_status="approved",
        ),
        "reviewer",
    )
    report = m.verify_acceptance(challenge, trust, (*receipts, review), now=now)
    assert report.cases[0].status == "PASS"
    assert (
        report.live_database_status
        == report.production_release_status
        == "NOT_VERIFIED"
    )
    unrelated = [r for r in receipts if r.evidence.payload.source != "host"]
    first = m.Operation(
        kind="claim",
        target=step.ref,
        outcome="accepted",
        at=now.timestamp(),
        lease_until=now.timestamp() + 10,
    )
    second = m.Operation(
        kind="claim", target=step.ref, outcome="unavailable", at=now.timestamp()
    )
    unrelated += [
        sign(
            m.HostEvidence(
                case_id="A", host_digest=host_ids[0], operations=(first, second)
            ),
            "host",
            0,
        ),
        sign(
            m.HostEvidence(
                case_id="A",
                host_digest=host_ids[1],
                operations=(
                    m.Operation(
                        kind="resume",
                        target=step.ref,
                        outcome="unavailable",
                        at=now.timestamp(),
                    ),
                ),
            ),
            "host",
            1,
        ),
    ]
    review_other = sign(
        m.ReviewEvidence(
            case_id="A",
            reviewed_evidence_digest=m.evidence_set_digest(unrelated),
            review_status="approved",
        ),
        "reviewer",
    )
    assert (
        m.verify_acceptance(challenge, trust, (*unrelated, review_other), now=now)
        .cases[0]
        .status
        == "NOT_VERIFIED"
    )
    without_fault = tuple(r for r in receipts if r.evidence.payload.source != "fault")
    result = m.verify_acceptance(challenge, trust, (*without_fault, review), now=now)
    assert result.cases[0].status == "NOT_VERIFIED"
    assert "FaultPhaseMissing" in result.cases[0].limitations
    assert "IndependentReviewMissing" in result.cases[0].limitations


@pytest.mark.parametrize(
    "status,code",
    [("unknown", "UnknownRedispatched"), ("succeeded", "CompletedRedispatched")],
)
def test_dispatch_receipt_is_checked_against_previous_database_state(
    ceremony, status, code
):
    m, now, challenge, trust, sign = ceremony
    step = m.StepFact(
        ref="1" * 64, run_ref="2" * 64, run_status="running", status="running"
    )
    action = m.ActionFact(
        ref="3" * 64, step_ref=step.ref, status=status, identity_digest="4" * 64
    )
    snapshot = m.DatabaseSnapshot(observed_at=now, steps=(step,), actions=(action,))
    host = next(s.host_digest for s in trust.signers if s.role == "host")
    receipts = (
        sign(m.DatabaseEvidence(case_id="E", phase="before", snapshot=snapshot)),
        sign(m.DatabaseEvidence(case_id="E", phase="after", snapshot=snapshot)),
        sign(
            m.HostEvidence(
                case_id="E",
                host_digest=host,
                operations=(
                    m.Operation(
                        kind="dispatch",
                        target=action.ref,
                        outcome="accepted",
                        at=now.timestamp(),
                    ),
                ),
            ),
            "host",
        ),
    )
    result = m.verify_acceptance(challenge, trust, receipts, now=now)
    assert result.cases[4].status == "FAIL"
    assert code in result.cases[4].observed_invariants


def test_retry_cooldown_summary_identity_and_cancellation_are_persistent_constraints(
    acceptance,
):
    m = acceptance
    now = datetime.now(timezone.utc)
    step = m.StepFact(
        ref="1" * 64,
        run_ref="2" * 64,
        run_status="running",
        status="ready",
        agent_status="ready_model",
        revision=5,
        counters=(1, 1, 0, 0),
        continuation=True,
        deferred_retry=True,
        available_at=now.timestamp() + 10,
        summary_identity="7" * 64,
    )
    attempt = m.AttemptFact(
        ref="3" * 64,
        step_ref=step.ref,
        call_ref="4" * 64,
        index=1,
        outcome="failed",
        identity_digest="5" * 64,
        created_at=now.timestamp() - 1,
    )
    before = m.DatabaseSnapshot(observed_at=now, steps=(step,), attempts=(attempt,))
    early = m.AttemptFact.model_validate(
        {
            **attempt.model_dump(),
            "ref": "6" * 64,
            "index": 2,
            "created_at": now.timestamp() + 1,
        }
    )
    after_step = m.StepFact.model_validate(
        {
            **step.model_dump(),
            "revision": 4,
            "counters": (0, 0, 0, 0),
            "summary_identity": "8" * 64,
        }
    )
    after = m.DatabaseSnapshot(
        observed_at=now, steps=(after_step,), attempts=(attempt, early)
    )
    assert {
        "RetryBeforeDeadline",
        "RevisionRollback",
        "BudgetRollback",
        "PersistentHistoryChanged",
    } <= m.transition_failures(before, after)
    cancelled = m.StepFact.model_validate(
        {
            **step.model_dump(),
            "run_status": "cancelled",
            "status": "cancelled",
            "agent_status": "cancelled",
            "deferred_retry": False,
        }
    )
    before = m.DatabaseSnapshot(observed_at=now, steps=(cancelled,))
    after = m.DatabaseSnapshot(
        observed_at=now,
        steps=(cancelled,),
        attempts=(early,),
        artifacts=(m.ArtifactFact(step_ref=step.ref, digest="9" * 64),),
    )
    assert {"CancelledLateCommit", "ArtifactDuplicate"} <= m.transition_failures(
        before, after
    )


def test_overlapping_quota_and_same_physical_host_do_not_pass(ceremony):
    m, now, challenge, trust, sign = ceremony
    provider = m.ProviderFact(
        ref="1" * 64,
        max_concurrent=1,
        max_requests=1,
        window_seconds=10.0,
        cooldown_until=0.0,
    )
    permits = tuple(
        m.PermitFact(
            ref=str(i) * 64,
            attempt_ref=str(i) * 64,
            provider_ref=provider.ref,
            status="active",
            created_at=now.timestamp(),
            expires_at=now.timestamp() + 10,
        )
        for i in (2, 3)
    )
    assert "AdmissionQuotaExceeded" in m.snapshot_failures(
        m.DatabaseSnapshot(observed_at=now, permits=permits, providers=(provider,))
    )
    hosts = [s.host_digest for s in trust.signers if s.role == "host"]
    infra = sign(
        m.InfrastructureEvidence(
            inventory_digest="2" * 64,
            host_failure_domains=((hosts[0], "3" * 64), (hosts[1], "3" * 64)),
        ),
        "infrastructure",
    )
    assert (
        m.verify_acceptance(challenge, trust, (infra,), now=now).cases[0].status
        == "FAIL"
    )


def test_acceptance_suite_seeds_one_bounded_mixed_batch(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from uuid import uuid4

    m = script(monkeypatch, "verify_rc_staging")
    import rc_workload

    created = []

    class Workload:
        def __init__(self, *args, **kwargs):
            pass

        def submit(self, scenario, *, staging_batch):
            created.append((scenario, staging_batch))
            return str(uuid4())

        def close(self):
            pass

    monkeypatch.setattr(rc_workload, "Workload", Workload)
    monkeypatch.setenv("AGENTFLOW_RC_DATABASE_URL", "unused-offline")
    monkeypatch.setenv("AGENTFLOW_RC_RECORDS", str(tmp_path / "records"))
    monkeypatch.delenv("AGENTFLOW_RC_ADMISSION_POLICY", raising=False)
    monkeypatch.delenv("AGENTFLOW_RC_OBSERVED_USAGE_POLICY", raising=False)
    args = SimpleNamespace(
        command="seed",
        plan=tmp_path / "private" / "plan.json",
        seconds=60,
        runs=1,
        scenario="acceptance_suite",
    )
    environment = "11111111-2222-4333-8444-555555555555"
    assert m.synthetic(SimpleNamespace(environment_id=environment), args) == 10
    assert {scenario for scenario, _ in created} == {
        "final",
        "normal",
        "review",
        "crash",
        "unknown",
        "retry",
        "cancel",
        "model_rate_limited",
        "summary",
        "summary_consumption",
    }
    assert len({batch for _, batch in created}) == 1
