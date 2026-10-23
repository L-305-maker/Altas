"""Complete synthetic signed ceremonies; no physical-host certification."""

from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_rc_acceptance import acceptance as acceptance
from test_rc_acceptance import ceremony as ceremony
from test_rc_tools import script


def legacy_bundle(ceremony, case):
    m, now, challenge, trust, sign = ceremony
    hosts = [s.host_digest for s in trust.signers if s.role == "host"]
    step = m.StepFact(
        ref="1" * 64, run_ref="2" * 64, run_status="running", status="ready"
    )
    other = m.StepFact.model_validate({**step.model_dump(), "ref": "4" * 64})
    before = m.DatabaseSnapshot(
        observed_at=now, database_backend="postgresql", steps=(step, other)
    )
    if case == "A":
        after = m.DatabaseSnapshot.model_validate(
            {
                **before.model_dump(),
                "steps": [
                    {
                        **step.model_dump(),
                        "status": "running",
                        "lease_digest": "3" * 64,
                        "lease_until": now.timestamp() + 10,
                    },
                    other.model_dump(),
                ],
            }
        )
    else:
        after = m.DatabaseSnapshot.model_validate(
            {
                **before.model_dump(),
                "attempts": [
                    dict(
                        ref="7" * 64,
                        step_ref=step.ref,
                        call_ref="8" * 64,
                        index=1,
                        outcome="inflight",
                        identity_digest="9" * 64,
                    )
                ],
                "providers": [
                    dict(
                        ref="5" * 64,
                        max_concurrent=1,
                        max_requests=2,
                        window_seconds=10.0,
                        cooldown_until=0.0,
                    )
                ],
                "permits": [
                    dict(
                        ref="6" * 64,
                        attempt_ref="7" * 64,
                        provider_ref="5" * 64,
                        status="active",
                        created_at=now.timestamp(),
                        expires_at=now.timestamp() + 10,
                    )
                ],
            }
        )
    receipts = [
        sign(
            m.BuildEvidence(
                identity=m.DeploymentIdentity(
                    git_commit_sha=challenge.deployment_sha,
                    application_version="1.0.0",
                    runtime_digest="1" * 64,
                    admission_digest="2" * 64,
                    usage_mapping_digest="3" * 64,
                    tool_digest="4" * 64,
                )
            ),
            "build",
        ),
        sign(
            m.InfrastructureEvidence(
                inventory_digest="1" * 64,
                host_failure_domains=((hosts[0], "1" * 64), (hosts[1], "2" * 64)),
            ),
            "infrastructure",
        ),
        sign(m.DatabaseEvidence(case_id=case, phase="before", snapshot=before)),
        sign(m.DatabaseEvidence(case_id=case, phase="after", snapshot=after)),
        sign(
            m.FaultEvidence(
                case_id=case, phase=m.PHASES[case], protected_record_digest="5" * 64
            ),
            "fault",
        ),
    ]
    for index, host in enumerate(hosts):
        op = m.Operation(
            kind="claim" if case == "A" else "model_reserve",
            target=step.ref if index == 0 else other.ref,
            outcome="accepted" if index == 0 else "unavailable",
            at=now.timestamp(),
            lease_until=now.timestamp() + 10 if case == "A" and index == 0 else None,
        )
        receipts.append(
            sign(
                m.HostEvidence(case_id=case, host_digest=host, operations=(op,)),
                "host",
                index,
            )
        )
    receipts.append(
        sign(
            m.ReviewEvidence(
                case_id=case,
                reviewed_evidence_digest=m.evidence_set_digest(receipts),
                review_status="approved",
            ),
            "reviewer",
        )
    )
    return receipts


@pytest.mark.parametrize("case", ("A", "F"))
def test_v2_false_positive_is_retained_only_as_legacy_evidence(ceremony, case):
    m, now, challenge, trust, _ = ceremony
    receipts = legacy_bundle(ceremony, case)
    result = m.verify_acceptance(challenge, trust, receipts, now=now)
    # These two assertions failed before implementation (v2 reported PASS).
    # Its historical meaning/signatures stay unchanged; v3 refuses v2 evidence.
    assert next(c for c in result.cases if c.case_id == case).status == "PASS"
    import rc_causal

    with pytest.raises(ValueError, match="EvidenceVersionMismatch"):
        rc_causal.verify_causal_acceptance(challenge, trust, receipts, now=now)


def digest(n):
    return f"{n:064x}"


@pytest.fixture
def causal(monkeypatch, acceptance):
    m = script(monkeypatch, "rc_causal")
    base = datetime.now(timezone.utc) - timedelta(seconds=30)
    stamp = base.timestamp()
    challenge = m.CausalChallenge(
        environment_id="11111111-2222-4333-8444-555555555555",
        deployment_sha="a" * 40,
        application_version="1.0.0",
        alembic_revision="0008",
        database_identity_digest=digest(90),
        staging_batch_digest=digest(91),
        v1_report_digest=digest(92),
        execution_nonce=digest(93),
        issued_at=base - timedelta(seconds=1),
        expires_at=base + timedelta(minutes=30),
    )
    keys, signers = {}, []
    for i, role in enumerate(
        ("database", "build", "infrastructure", "fault", "reviewer", "host", "host")
    ):
        key = Ed25519PrivateKey.generate()
        public = key.public_key().public_bytes_raw().hex()
        identity = m.request_digest(public)
        keys[identity] = key
        signers.append(
            acceptance.TrustedSigner(
                key_id=identity,
                public_key=public,
                role=role,
                host_digest=digest(i) if role == "host" else None,
                not_before=base - timedelta(days=1),
                not_after=base + timedelta(days=1),
            )
        )
    trust = acceptance.AcceptanceTrust(version=1, signers=signers)
    hosts = [s.host_digest for s in signers if s.role == "host"]

    def sign(
        payload,
        role="database",
        index=0,
        start=0,
        end=None,
        scope=None,
        legacy_domain=False,
    ):
        signer = [s for s in signers if s.role == role][index]
        evidence = m.CausalEvidence(
            challenge=scope or challenge,
            attestation_identity=signer.key_id,
            started_at=base + timedelta(seconds=start),
            completed_at=base + timedelta(seconds=start if end is None else end),
            payload=payload,
        )
        return m.CausalSignedEvidence(
            evidence=evidence,
            evidence_digest=m.request_digest(evidence.model_dump(mode="json")),
            signature=keys[signer.key_id]
            .sign(
                acceptance.signing_bytes(evidence)
                if legacy_domain
                else m.signing_bytes(evidence)
            )
            .hex(),
        )

    def bundle(case, mutate=None, *, review=True, scope=None):
        step = dict(
            ref=digest(10), run_ref=digest(20), run_status="running", status="ready"
        )
        other = {**step, "ref": digest(11)}
        before = dict(
            steps=[step, other], database_backend="postgresql", observed_at=base
        )
        after = {
            **before,
            "steps": [dict(step), dict(other)],
            "observed_at": base + timedelta(seconds=25),
        }
        targets = [step["ref"]]
        op = dict(
            kind="resume",
            target=step["ref"],
            target_step_ref=step["ref"],
            outcome="accepted",
            at=stamp + 5,
        )
        ops = [
            {**op, "host_identity": hosts[0]},
            {**op, "at": stamp + 6, "host_identity": hosts[1]},
        ]
        action = dict(
            ref=digest(30),
            step_ref=step["ref"],
            status="inflight",
            identity_digest=digest(31),
            idempotency_digest=digest(32),
            binding_digest=digest(33),
        )
        pool = dict(
            ref=digest(40),
            max_concurrent=1,
            max_requests=2,
            window_seconds=10.0,
            cooldown_until=0.0,
        )
        call = dict(
            ref=digest(50),
            step_ref=step["ref"],
            round_index=1,
            attempts=1,
            status="inflight",
        )
        attempt = dict(
            ref=digest(60),
            step_ref=step["ref"],
            call_ref=call["ref"],
            index=1,
            outcome="inflight",
            identity_digest=digest(61),
            created_at=stamp + 5,
            provider_ref=pool["ref"],
            contract_digest=digest(62),
        )
        permit = dict(
            ref=digest(70),
            attempt_ref=attempt["ref"],
            provider_ref=pool["ref"],
            status="active",
            created_at=stamp + 5,
            expires_at=stamp + 60,
        )
        if case in {"A", "B", "C", "I", "J"}:
            if case != "A":
                before["steps"][0] = {
                    **step,
                    "status": "running",
                    "lease_digest": digest(80),
                    "lease_until": stamp + 4,
                }
            after["steps"][0].update(
                status="running", lease_digest=digest(81), lease_until=stamp + 60
            )
        if case == "A":
            ops[0].update(kind="claim", lease_digest=digest(81), lease_until=stamp + 60)
            ops[1].update(kind="claim", outcome="unavailable")
        elif case == "B":
            before["steps"][0]["lease_until"] = stamp + 8
            before.update(actions=[action])
            after.update(actions=[{**action, "status": "unknown"}])
            after["steps"][0].update(
                status="waiting",
                agent_status="waiting_reconciliation",
                lease_digest=None,
                lease_until=None,
            )
            ops[0].update(
                kind="worker_exit",
                outcome="safe_stopped",
                action_ref=action["ref"],
                lease_digest=digest(80),
            )
        elif case == "C":
            ops[0].update(
                kind="stale_commit",
                outcome="rejected",
                error_code="LostLease",
                lease_digest=digest(80),
                at=stamp + 7,
            )
            ops[1].update(kind="claim", lease_digest=digest(81), lease_until=stamp + 60)
        elif case == "D":
            action.update(status="succeeded", consumed_at=stamp - 1)
            approval = dict(
                ref=digest(34),
                action_ref=action["ref"],
                binding_digest=action["binding_digest"],
                decision="approved",
                consumed_at=stamp - 1,
            )
            before.update(actions=[action], approvals=[approval])
            after.update(actions=[dict(action)], approvals=[dict(approval)])
            for o in ops:
                o.update(action_ref=action["ref"], approval_ref=approval["ref"])
        elif case == "E":
            action.update(status="unknown")
            before.update(actions=[action])
            after.update(
                actions=[
                    {**action, "status": "succeeded", "reconciled_at": stamp + 10}
                ],
                reconciliations=[
                    dict(
                        ref=digest(35),
                        action_ref=action["ref"],
                        previous_status="unknown",
                        confirmed_status="succeeded",
                        applied=True,
                        confirmed_at=stamp + 10,
                    )
                ],
            )
            for i, o in enumerate(ops):
                o.update(
                    action_ref=action["ref"],
                    reconciliation_ref=digest(35),
                    at=stamp + 12 + i,
                )
        elif case == "F":
            targets.append(other["ref"])
            before.update(providers=[pool], global_provider_scope=[pool["ref"]])
            after.update(
                providers=[dict(pool)],
                global_provider_scope=[pool["ref"]],
                attempts=[attempt],
                permits=[permit],
                calls=[call],
            )
            ops[0].update(
                kind="model_reserve",
                provider_ref=pool["ref"],
                model_call_ref=call["ref"],
                attempt_ref=attempt["ref"],
                permit_ref=permit["ref"],
            )
            ops[1].update(
                kind="model_reserve",
                target=other["ref"],
                target_step_ref=other["ref"],
                outcome="unavailable",
                provider_ref=pool["ref"],
                error_code="ProviderAdmissionDeferred",
            )
        elif case == "G":
            pool.update(cooldown_until=stamp + 11)
            attempt.update(outcome="failed", created_at=stamp - 2)
            call.update(status="failed")
            before.update(providers=[pool], attempts=[attempt], calls=[call])
            before["steps"][0].update(
                agent_status="ready_model",
                continuation=True,
                deferred_retry=True,
                available_at=stamp + 12,
            )
            after.update(
                providers=[dict(pool)],
                calls=[{**call, "status": "succeeded", "attempts": 2}],
                attempts=[
                    dict(attempt),
                    {
                        **attempt,
                        "ref": digest(63),
                        "index": 2,
                        "outcome": "succeeded",
                        "created_at": stamp + 15,
                    },
                ],
            )
            ops[0].update(
                kind="rate_limited",
                outcome="rejected",
                model_call_ref=call["ref"],
                attempt_ref=attempt["ref"],
                provider_ref=pool["ref"],
            )
            ops[1].update(
                kind="model_reserve",
                at=stamp + 15,
                model_call_ref=call["ref"],
                attempt_ref=digest(63),
                provider_ref=pool["ref"],
            )
        elif case == "H":
            targets = [digest(10), digest(11), digest(12)]
            before["steps"] = [
                {**step, "status": "succeeded"},
                {
                    **other,
                    "status": "succeeded",
                    "summary_identity": digest(100),
                    "artifact_required": True,
                },
                {**step, "ref": digest(12)},
            ]
            after["steps"] = [dict(s) for s in before["steps"]]
            after["steps"][2]["consumer_identity"] = digest(101)
            before.update(
                artifacts=[dict(step_ref=digest(11), digest=digest(100))],
                calls=[{**call, "step_ref": digest(11), "status": "succeeded"}],
                attempts=[
                    {
                        **attempt,
                        "step_ref": digest(11),
                        "outcome": "succeeded",
                        "created_at": stamp - 5,
                        "summary_input_digest": digest(104),
                    }
                ],
            )
            after.update(
                artifacts=before["artifacts"],
                calls=before["calls"]
                + [{**call, "ref": digest(51), "step_ref": digest(12)}],
                attempts=before["attempts"]
                + [
                    {
                        **attempt,
                        "ref": digest(64),
                        "step_ref": digest(12),
                        "call_ref": digest(51),
                        "contract_digest": digest(65),
                        "summary_consumption_digest": digest(101),
                    }
                ],
                summary_links=[
                    dict(
                        source_step_ref=digest(10),
                        generator_step_ref=digest(11),
                        consumer_step_ref=digest(12),
                        source_snapshot_digest=digest(102),
                        summary_request_digest=digest(103),
                        summary_input_digest=digest(104),
                        artifact_digest=digest(100),
                        view_digest=digest(101),
                        generator_contract_digest=digest(62),
                        consumer_contract_digest=digest(65),
                    )
                ],
            )
            ops[0].update(target=digest(11), target_step_ref=digest(11))
            ops[1].update(target=digest(12), target_step_ref=digest(12))
        elif case == "I":
            after["steps"][0].update(
                status="cancelled",
                run_status="cancelled",
                agent_status="cancelled",
                lease_digest=None,
                lease_until=None,
            )
            after["steps"][1].update(status="cancelled", run_status="cancelled")
            ops[0].update(
                kind="cancelled_commit",
                outcome="rejected",
                error_code="LostLease",
                lease_digest=digest(80),
            )
            ops[1].update(outcome="rejected", error_code="LostLease")
        elif case == "J":
            ops[0].update(
                kind="database_unavailable",
                target=challenge.database_identity_digest,
                outcome="safe_stopped",
                error_code="DatabaseUnavailable",
            )
            ops[1].update(
                kind="database_recovered",
                target=challenge.database_identity_digest,
                at=stamp + 15,
            )
        data = dict(
            before=before,
            after=after,
            ops=ops,
            targets=targets,
            provider=pool["ref"] if case == "F" else None,
        )
        if mutate:
            mutate(data)
        b, a = (
            m.CausalSnapshot.model_validate(data["before"]),
            m.CausalSnapshot.model_validate(data["after"]),
        )
        receipts = [
            sign(
                acceptance.BuildEvidence(
                    identity=acceptance.DeploymentIdentity(
                        git_commit_sha=challenge.deployment_sha,
                        application_version="1.0.0",
                        runtime_digest=digest(1),
                        admission_digest=digest(2),
                        usage_mapping_digest=digest(3),
                        tool_digest=digest(4),
                    )
                ),
                "build",
            ),
            sign(
                acceptance.InfrastructureEvidence(
                    inventory_digest=digest(1),
                    host_failure_domains=((hosts[0], digest(1)), (hosts[1], digest(2))),
                ),
                "infrastructure",
            ),
            sign(m.CausalDatabaseEvidence(case_id=case, phase="before", snapshot=b)),
            sign(
                m.CausalDatabaseEvidence(case_id=case, phase="after", snapshot=a),
                start=25,
            ),
        ]
        fault = sign(
            m.CausalFault(
                case_id=case,
                phase=acceptance.PHASES[case],
                protected_record_digest=digest(3),
                target_step_refs=data["targets"],
                provider_ref=data["provider"],
            ),
            "fault",
            start=1,
            end=20,
        )
        receipts.append(fault)
        for i, op in enumerate(data["ops"]):
            values = dict(
                evidence_version=3,
                environment_id=str(challenge.environment_id),
                staging_batch_digest=challenge.staging_batch_digest,
                case_id=case,
                causal_group_id=m.causal_group(
                    challenge, case, b, a, fault.evidence_digest
                ),
                database_before_digest=m.request_digest(b.model_dump(mode="json")),
                database_after_digest=m.request_digest(a.model_dump(mode="json")),
                fault_receipt_digest=fault.evidence_digest,
                **op,
            )
            for name, field in m.RCCausalWitness.model_fields.items():
                if not field.is_required():
                    values.setdefault(name, field.default)
            witness = m.RCCausalWitness(
                **values, witness_digest=m.request_digest(values)
            )
            receipts.append(
                sign(
                    m.CausalHost(
                        case_id=case,
                        host_digest=op["host_identity"],
                        operations=(witness,),
                    ),
                    "host",
                    i,
                    start=1,
                    end=20,
                    scope=scope,
                )
            )
        if review:
            receipts.append(
                sign(
                    acceptance.ReviewEvidence(
                        case_id=case,
                        reviewed_evidence_digest=acceptance.evidence_set_digest(
                            receipts
                        ),
                        review_status="approved",
                    ),
                    "reviewer",
                    start=26,
                )
            )
        return receipts

    return m, challenge, trust, base + timedelta(seconds=30), bundle, sign


def case_result(causal, case, mutate=None, **kwargs):
    m, challenge, trust, now, bundle, _ = causal
    return next(
        c
        for c in m.verify_causal_acceptance(
            challenge, trust, bundle(case, mutate, **kwargs), now=now
        ).cases
        if c.case_id == case
    )


@pytest.mark.parametrize("case", tuple("ABCDEFGHIJ"))
def test_complete_signed_v3_causal_chain_passes(causal, case):
    result = case_result(causal, case)
    assert result.status == "PASS", result.model_dump()


@pytest.mark.parametrize(
    "case,change",
    [
        (
            "A",
            lambda d: d["ops"][1].update(target=digest(11), target_step_ref=digest(11)),
        ),
        ("F", lambda d: d["ops"][1].update(provider_ref=digest(41))),
        ("F", lambda d: d["ops"][1].update(at=d["ops"][0]["at"] + 10)),
        ("F", lambda d: d["ops"][0].update(attempt_ref=digest(69))),
        ("B", lambda d: d["ops"][0].update(lease_digest=digest(89))),
        (
            "C",
            lambda d: d["ops"][0].update(target=digest(11), target_step_ref=digest(11)),
        ),
        ("D", lambda d: [o.update(action_ref=digest(39)) for o in d["ops"]]),
        ("E", lambda d: d["after"]["reconciliations"][0].update(applied=False)),
        ("G", lambda d: d["ops"][1].update(model_call_ref=digest(59))),
        (
            "H",
            lambda d: d["after"]["summary_links"][0].update(
                generator_step_ref=digest(10)
            ),
        ),
        (
            "I",
            lambda d: [
                o.update(target=digest(11), target_step_ref=digest(11))
                for o in d["ops"]
            ],
        ),
        ("J", lambda d: d["ops"][1].update(kind="resume", target=digest(10))),
    ],
)
def test_signed_but_unrelated_or_incomplete_causality_never_passes(
    causal, case, change
):
    assert case_result(causal, case, change).status != "PASS"


def test_overlapping_claims_and_global_out_of_batch_quota_are_failures(causal):
    def overlap(d):
        d["ops"][1].update(
            outcome="accepted",
            lease_digest=digest(82),
            lease_until=d["ops"][0]["lease_until"],
        )

    result = case_result(causal, "A", overlap)
    assert result.status == "FAIL" and "LeaseOverlap" in result.observed_invariants

    def overflow(d):
        p = d["after"]["permits"][0]
        d["after"]["permits"].append(
            {**p, "ref": digest(71), "attempt_ref": digest(66)}
        )

    result = case_result(causal, "F", overflow)
    assert (
        result.status == "FAIL"
        and "AdmissionQuotaExceeded" in result.observed_invariants
    )


@pytest.mark.parametrize("missing", (True, False))
def test_independent_review_must_cover_the_entire_set(causal, missing):
    m, challenge, trust, now, bundle, sign = causal
    receipts = bundle("A", review=False)
    if not missing:
        from rc_acceptance import ReviewEvidence

        receipts.append(
            sign(
                ReviewEvidence(
                    case_id="A",
                    reviewed_evidence_digest=digest(99),
                    review_status="approved",
                ),
                "reviewer",
                start=26,
            )
        )
    result = m.verify_causal_acceptance(challenge, trust, receipts, now=now)
    assert result.cases[0].status == "NOT_VERIFIED"
    assert "IndependentReviewMissing" in result.cases[0].limitations


def test_cross_case_rewrapped_event_and_duplicate_receipt_are_rejected(causal):
    m, challenge, trust, now, bundle, sign = causal
    receipts = bundle("A")
    host = receipts[-2].evidence.payload
    cross = m.CausalHost.model_validate({**host.model_dump(), "case_id": "C"})
    with pytest.raises(ValueError, match="CausalEventReplay"):
        m.verify_causal_acceptance(
            challenge,
            trust,
            [*receipts, sign(cross, "host", 1, start=1, end=20)],
            now=now,
        )
    with pytest.raises(ValueError, match="CausalEventReplay"):
        m.verify_causal_acceptance(challenge, trust, [*receipts, receipts[-2]], now=now)


@pytest.mark.parametrize(
    "field,value",
    [("staging_batch_digest", digest(999)), ("execution_nonce", digest(998))],
)
def test_signed_event_cannot_cross_challenge_or_batch(causal, field, value):
    m, challenge, trust, now, bundle, _ = causal
    other = m.CausalChallenge.model_validate({**challenge.model_dump(), field: value})
    with pytest.raises(ValueError, match="EvidenceScopeMismatch"):
        m.verify_causal_acceptance(challenge, trust, bundle("A", scope=other), now=now)


def test_v3_signatures_roles_keys_and_registered_challenge(causal, tmp_path):
    m, challenge, trust, now, bundle, sign = causal
    receipts = bundle("A")
    bad = m.CausalSignedEvidence.model_validate(
        {**receipts[0].model_dump(), "signature": "0" * 128}
    )
    with pytest.raises(ValueError, match="EvidenceSignatureRejected"):
        m.verify_causal_acceptance(challenge, trust, [bad, *receipts[1:]], now=now)
    wrong_role = sign(receipts[0].evidence.payload, "database")
    with pytest.raises(ValueError, match="UntrustedEvidenceSigner"):
        m.verify_causal_acceptance(
            challenge, trust, [wrong_role, *receipts[1:]], now=now
        )
    empty_trust = type(trust)(version=2, signers=())
    with pytest.raises(ValueError, match="UntrustedEvidenceSigner"):
        m.verify_causal_acceptance(challenge, empty_trust, receipts, now=now)
    from rc_acceptance import consume_challenge, issue_challenge

    path = tmp_path / "private-v3"
    issue_challenge(path, challenge)
    consume_challenge(path, challenge)
    with pytest.raises(ValueError, match="ChallengeReplay"):
        consume_challenge(path, challenge)


def test_v2_and_v3_signature_domains_and_historical_report(causal, ceremony):
    m, challenge, trust, now, bundle, _ = causal
    legacy, old_now, old_challenge, old_trust, _ = ceremony
    receipts = bundle("A")
    assert m.signing_bytes(receipts[0].evidence).startswith(
        b"Altas.RCCausalEvidence.v3\n"
    )
    assert legacy.signing_bytes(receipts[0].evidence) != m.signing_bytes(
        receipts[0].evidence
    )
    old = legacy_bundle(ceremony, "A")
    report = legacy.verify_acceptance(old_challenge, old_trust, old, now=old_now)
    assert (
        legacy.RCIndependentAcceptance.model_validate_json(report.model_dump_json())
        == report
    )
    assert legacy.SignedEvidence.model_validate_json(old[0].model_dump_json()) == old[0]
    with pytest.raises(ValueError, match="EvidenceVersionMismatch"):
        legacy.verify_acceptance(challenge, trust, receipts, now=now)


def test_witness_scope_and_schema_do_not_allow_untrusted_content(causal):
    m, challenge, trust, now, bundle, sign = causal
    receipts = bundle("A")
    host = receipts[-2].evidence.payload
    values = host.operations[0].model_dump(mode="json", exclude={"witness_digest"})
    values["case_id"] = "F"
    op = m.RCCausalWitness(**values, witness_digest=m.request_digest(values))
    altered = m.CausalHost(case_id="A", host_digest=host.host_digest, operations=(op,))
    with pytest.raises(ValueError, match="CausalScopeMismatch"):
        m.verify_causal_acceptance(
            challenge,
            trust,
            [*receipts[:-2], sign(altered, "host", 1, start=1, end=20), receipts[-1]],
            now=now,
        )
    for field in ("prompt", "secret", "token", "url"):
        with pytest.raises(ValueError):
            m.RCCausalWitness.model_validate(
                {**op.model_dump(), field: "PRIVATE_VALUE"}
            )


def test_cancelled_target_cannot_disagree_with_causal_step(causal):
    assert (
        case_result(causal, "I", lambda d: d["ops"][0].update(target=digest(11))).status
        != "PASS"
    )


def test_changed_persistent_run_identity_is_not_a_valid_witness(causal):
    assert (
        case_result(
            causal, "A", lambda d: d["after"]["steps"][0].update(run_ref=digest(29))
        ).status
        == "FAIL"
    )


def test_two_real_reservations_in_different_pools_do_not_prove_shared_admission(causal):
    def different_pools(d):
        a = {
            **d["after"]["attempts"][0],
            "ref": digest(67),
            "step_ref": digest(11),
            "call_ref": digest(51),
            "provider_ref": digest(41),
        }
        d["after"]["attempts"].append(a)
        d["after"]["calls"].append(
            {**d["after"]["calls"][0], "ref": digest(51), "step_ref": digest(11)}
        )
        p = {
            **d["after"]["permits"][0],
            "ref": digest(71),
            "attempt_ref": a["ref"],
            "provider_ref": digest(41),
        }
        d["after"]["permits"].append(p)
        for phase in ("before", "after"):
            d[phase]["providers"].append(
                {**d[phase]["providers"][0], "ref": digest(41)}
            )
            d[phase]["global_provider_scope"].append(digest(41))
        d["ops"][1].update(
            outcome="accepted",
            at=a["created_at"],
            provider_ref=digest(41),
            attempt_ref=a["ref"],
            permit_ref=p["ref"],
            model_call_ref=a["call_ref"],
            error_code=None,
        )

    assert case_result(causal, "F", different_pools).status == "NOT_VERIFIED"


def test_competition_scope_does_not_make_different_steps_causal(causal):
    def different_steps(d):
        d["targets"].append(digest(11))
        d["ops"][1].update(target=digest(11), target_step_ref=digest(11))

    assert case_result(causal, "A", different_steps).status == "NOT_VERIFIED"


def test_v2_domain_signature_is_rejected_by_v3(causal):
    m, challenge, trust, now, bundle, sign = causal
    receipts = bundle("A")
    receipts[0] = sign(receipts[0].evidence.payload, "build", legacy_domain=True)
    with pytest.raises(ValueError, match="EvidenceSignatureRejected"):
        m.verify_causal_acceptance(challenge, trust, receipts, now=now)


def test_fault_window_is_narrower_than_database_envelopes(causal):
    m, challenge, trust, now, bundle, sign = causal
    receipts = bundle("A")
    host = receipts[-2].evidence.payload
    values = host.operations[0].model_dump(mode="json", exclude={"witness_digest"})
    values["at"] += 15  # Snapshot interval still includes it; fault interval does not.
    op = m.RCCausalWitness(**values, witness_digest=m.request_digest(values))
    host = m.CausalHost(case_id="A", host_digest=host.host_digest, operations=(op,))
    receipts[-2] = sign(host, "host", 1, start=1, end=24)
    result = m.verify_causal_acceptance(challenge, trust, receipts, now=now)
    assert result.cases[0].status == "NOT_VERIFIED"


def test_v3_bounded_fact_and_evidence_inputs(causal):
    m, challenge, trust, now, bundle, _ = causal
    receipts = bundle("F")
    snapshot = receipts[3].evidence.payload.snapshot
    with pytest.raises(ValueError):
        m.CausalSnapshot.model_validate(
            {**snapshot.model_dump(), "calls": list(snapshot.calls) * 2001}
        )
    with pytest.raises(ValueError, match="EvidenceBoundsExceeded"):
        m.verify_causal_acceptance(challenge, trust, receipts * 13, now=now)


def test_real_reservation_timestamps_need_not_equal_receipt_completion(causal):
    def clocks(d):
        at = d["ops"][0]["at"]
        d["after"]["permits"][0]["created_at"] = at - 0.002
        d["after"]["attempts"][0]["created_at"] = at - 0.001

    assert case_result(causal, "F", clocks).status == "PASS"


def test_exit_after_lease_expiry_cannot_prove_holder_crash(causal):
    def expired(d):
        d["before"]["steps"][0]["lease_until"] = d["ops"][0]["at"] - 1

    assert case_result(causal, "B", expired).status == "NOT_VERIFIED"


def test_takeover_before_old_lease_expiry_is_a_violation(causal):
    def overlap(d):
        d["before"]["steps"][0]["lease_until"] = d["ops"][1]["at"] + 1

    result = case_result(causal, "C", overlap)
    assert result.status == "FAIL" and "LeaseOverlap" in result.observed_invariants


def test_inflight_action_keeps_binding_and_idempotency_identity(causal):
    assert (
        case_result(
            causal,
            "B",
            lambda d: d["after"]["actions"][0].update(idempotency_digest=digest(333)),
        ).status
        == "FAIL"
    )


def test_v3_script_import_is_also_an_explicit_v3_entry(monkeypatch, capsys):
    module = script(monkeypatch, "verify_rc_causal")
    monkeypatch.setattr("sys.argv", ["verify_rc_causal.py", "verify"])
    assert module.main() == 2
    import json

    output = capsys.readouterr().out
    assert json.loads(output)["schema_version"] == 3
    assert "PRIVATE" not in output
