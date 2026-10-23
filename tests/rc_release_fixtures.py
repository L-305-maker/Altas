"""Synthetic signing keys stay in memory; these bundles certify no real hosts."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class ReleaseBundle:
    def __init__(self, root, *, plan=None):
        import rc_acceptance as a
        import rc_host_witness as m
        import rc_staging as s

        self.a, self.m, self.s = a, m, s
        self.root = Path(root)
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()
        self.base = datetime.now(timezone.utc) - timedelta(seconds=40)
        environment = "11111111-2222-4333-8444-555555555555"
        self.plan = plan or dict(
            environment=environment,
            batch_id=environment,
            runs=[
                dict(id=str(UUID(int=i + 1)), scenario=scenario)
                for i, scenario in enumerate(
                    (
                        "final",
                        "crash",
                        "normal",
                        "review",
                        "unknown",
                        "final",
                        "retry",
                        "summary_consumption",
                        "cancel",
                        "normal",
                    )
                )
            ],
        )
        self.keys, signers = {}, []
        for i, role in enumerate(
            ("database", "build", "infrastructure", "fault", "reviewer", "host", "host")
        ):
            key = Ed25519PrivateKey.generate()
            public = key.public_key().public_bytes_raw().hex()
            identity = m.request_digest(public)
            self.keys[identity] = key
            signers.append(
                a.TrustedSigner(
                    key_id=identity,
                    public_key=public,
                    role=role,
                    host_digest=str(i) * 64 if role == "host" else None,
                    not_before=self.base - timedelta(days=1),
                    not_after=self.base + timedelta(days=1),
                )
            )
        self.trust = a.AcceptanceTrust(version=1, signers=signers)
        self.identity = s.DeploymentIdentity(
            git_commit_sha="a" * 40,
            application_version="1.0.0",
            runtime_digest="1" * 64,
            admission_digest="2" * 64,
            usage_mapping_digest="3" * 64,
            tool_digest="4" * 64,
        )
        self.config = s.StagingConfiguration(
            environment_id=self.plan["environment"],
            database_oid=12345,
            expected_identity=self.identity,
            hosts=[
                s.HostIdentity(
                    role=role, host_digest=digit * 64, deployment=self.identity
                )
                for role, digit in (("api", "9"), ("worker", "5"), ("worker", "6"))
            ],
        )
        facts = s.DatabaseFacts(
            revision="0008",
            environment_matches=True,
            database_matches=True,
            tls=True,
            safe_role=True,
            permissions=True,
            timeouts=True,
        )
        original = s.report(
            self.config,
            facts=facts,
            errors=(),
            started=self.base - timedelta(seconds=2),
            identity=self.identity,
        )
        self.original = s.RCStagingEvidenceReport.model_validate(
            {**original.model_dump(), "completed_at": self.base - timedelta(seconds=2)}
        )
        self.challenge = m.CausalChallenge(
            environment_id=self.config.environment_id,
            deployment_sha=self.identity.git_commit_sha,
            application_version="1.0.0",
            alembic_revision="0008",
            database_identity_digest="b" * 64,
            staging_batch_digest=m.checked_plan(self.plan, self.config.environment_id),
            v1_report_digest=m.request_digest(self.original.model_dump(mode="json")),
            execution_nonce="e" * 64,
            issued_at=self.base - timedelta(seconds=1),
            expires_at=self.base + timedelta(minutes=30),
        )
        self.registry = self.root / "registry"
        a.issue_challenge(self.registry, self.challenge)
        for name, value in (
            ("config.json", self.config),
            ("v1.json", self.original),
            ("challenge.json", self.challenge),
            ("trust.json", self.trust),
        ):
            self.write(self.root / name, value)
        self.write(self.root / "plan.json", self.plan)
        self.args = SimpleNamespace(
            command="precheck",
            config=str(self.root / "config.json"),
            v1_report=str(self.root / "v1.json"),
            plan=str(self.root / "plan.json"),
            challenge=str(self.root / "challenge.json"),
            state_dir=str(self.registry),
            trust=str(self.root / "trust.json"),
            trust_sha256=hashlib.sha256(
                (self.root / "trust.json").read_bytes()
            ).hexdigest(),
            collector_sha256="c" * 64,
            bundle=str(self.bundle),
        )
        self.write(
            self.bundle / "build.json",
            self.sign(a.BuildEvidence(identity=self.identity), "build"),
        )
        self.write(
            self.bundle / "infrastructure.json",
            self.sign(
                a.InfrastructureEvidence(
                    inventory_digest="f" * 64,
                    host_failure_domains=[("5" * 64, "1" * 64), ("6" * 64, "2" * 64)],
                ),
                "infrastructure",
            ),
        )

    def write(self, path, value):
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        Path(path).write_text(self.m.canonical_json(value), encoding="utf-8")

    def sign(self, payload, role="database", index=0, *, start=None, end=None):
        from rc_causal import signing_bytes

        signer = [s for s in self.trust.signers if s.role == role][index]
        evidence_type = self.m.CausalSignedEvidence.model_fields["evidence"].annotation
        evidence = evidence_type(
            evidence_version=3,
            challenge=self.challenge,
            attestation_identity=signer.key_id,
            started_at=start or self.base,
            completed_at=end or start or self.base,
            payload=payload,
        )
        return self.m.CausalSignedEvidence(
            evidence=evidence,
            evidence_digest=self.m.request_digest(evidence.model_dump(mode="json")),
            signature=self.keys[signer.key_id].sign(signing_bytes(evidence)).hex(),
        )

    def receipts(self):
        return [
            self.m.CausalSignedEvidence.model_validate_json(p.read_bytes())
            for p in sorted(self.bundle.glob("*.json"))
            if p.name in {"build.json", "infrastructure.json"}
        ] + [
            self.m.CausalSignedEvidence.model_validate_json(p.read_bytes())
            for case in "ABCDEFGHIJ"
            for p in sorted((self.bundle / case).glob("*.json"))
            if p.name != "binding.json"
        ]

    def sources(self, case):
        return [
            r
            for r in self.receipts()
            if r.evidence.payload.source in {"build", "infrastructure"}
            or (
                getattr(r.evidence.payload, "case_id", None) == case
                and r.evidence.payload.source != "reviewer"
            )
        ]

    def review(self, case):
        sources = self.sources(case)
        completed = max(r.evidence.completed_at for r in sources)
        self.write(
            self.bundle / case / "reviewer.json",
            self.sign(
                self.a.ReviewEvidence(
                    case_id=case,
                    review_status="approved",
                    reviewed_evidence_digest=self.a.evidence_set_digest(sources),
                ),
                "reviewer",
                start=max(
                    self.base + timedelta(seconds=30),
                    completed + timedelta(microseconds=1),
                ),
            ),
        )

    def add_case(
        self, case, before, after, observations, *, fault=None, collector_headers=None
    ):
        from rc_causal import CausalDatabaseEvidence, CausalFault

        directory = self.bundle / case
        directory.mkdir()
        hosts = [s.host_digest for s in self.trust.signers if s.role == "host"]
        for phase, snapshot in (("before", before), ("after", after)):
            self.write(
                directory / f"{phase}.json",
                self.sign(
                    CausalDatabaseEvidence(
                        case_id=case, phase=phase, snapshot=snapshot
                    ),
                    start=snapshot.observed_at,
                ),
            )
        journals, headers = [], []
        for i, entries in enumerate(observations):
            root = directory / f"host-{i}"
            root.mkdir(mode=0o700)
            header = self.m.CollectorHeader(
                challenge_digest=self.m.request_digest(
                    self.challenge.model_dump(mode="json")
                ),
                environment_id=self.challenge.environment_id,
                staging_batch_digest=self.challenge.staging_batch_digest,
                case_id=case,
                host_identity=hosts[i],
                process_id=100 + i,
                process_nonce=self.m.request_digest((case, i))[:32],
                deployment_sha=self.challenge.deployment_sha,
                collector_digest=self.args.collector_sha256,
                initial_snapshot_digest=self.m.request_digest(
                    before.model_dump(mode="json")
                ),
                started_at=before.observed_at,
            )
            if collector_headers is not None:
                header = self.m.CollectorHeader.model_validate(collector_headers[i])
            self.write(root / "header.json", header)
            last = self.m.request_digest(header.model_dump(mode="json"))
            for index, observation in enumerate(entries):
                value = dict(
                    sequence=index,
                    previous_digest=last,
                    observation=observation.model_dump(mode="json"),
                )
                last = self.m.request_digest(value)
                self.write(
                    root / f"event-{index:04d}.json", {**value, "record_digest": last}
                )
            self.write(
                root / "closed.json",
                dict(events=len(entries), journal_digest=last, complete=True),
            )
            journals.append((hosts[i], last))
            headers.append(self.m.request_digest(header.model_dump(mode="json")))
        binding = self.m.ProvenanceBinding(
            challenge_digest=self.m.request_digest(
                self.challenge.model_dump(mode="json")
            ),
            case_id=case,
            journals=journals,
            process_headers=headers,
            fault_record_digest=self.m.request_digest((case, "fault")),
        )
        self.write(directory / "binding.json", binding)
        fault = fault or CausalFault(
            case_id=case,
            phase=self.a.PHASES[case],
            target_step_refs=[s.ref for s in after.steps][:3],
            provider_ref=after.providers[0].ref if case == "F" else None,
            protected_record_digest=self.m.request_digest(
                binding.model_dump(mode="json")
            ),
        )
        fault = fault.model_copy(
            update={
                "protected_record_digest": self.m.request_digest(
                    binding.model_dump(mode="json")
                )
            }
        )
        self.write(
            directory / "fault.json",
            self.sign(
                fault,
                "fault",
                start=before.observed_at
                + timedelta(
                    seconds=1
                    if (after.observed_at - before.observed_at).total_seconds() > 2
                    else 0
                ),
                end=after.observed_at
                - timedelta(
                    seconds=1
                    if (after.observed_at - before.observed_at).total_seconds() > 2
                    else 0
                ),
            ),
        )
        for i in range(len(observations)):
            draft = self.m.unsigned_draft(
                directory / f"host-{i}",
                self.challenge,
                self.registry,
                self.trust,
                self.sources(case),
                binding,
            )
            self.write(
                directory / f"host-{i}.json",
                self.sign(
                    draft["payload"],
                    "host",
                    i,
                    start=datetime.fromisoformat(draft["started_at"]),
                    end=datetime.fromisoformat(draft["completed_at"]),
                ),
            )
        self.review(case)

    def change_host(self, case, index, change):
        path = self.bundle / case / f"host-{index}.json"
        receipt = self.m.CausalSignedEvidence.model_validate_json(path.read_bytes())
        value = receipt.evidence.model_dump(mode="json")
        change(value)
        self.write(
            path,
            self.sign(
                value["payload"],
                "host",
                index,
                start=value["started_at"],
                end=value["completed_at"],
            ),
        )
        self.review(case)

    def change_journal(self, case, index, change):
        """Rewrite the entire unsigned chain; protected signatures stay fixed."""
        root = self.bundle / case / f"host-{index}"
        header = json.loads((root / "header.json").read_text("utf-8"))
        event = json.loads((root / "event-0000.json").read_text("utf-8"))
        change(header, event["observation"])
        self.write(root / "header.json", header)
        event["previous_digest"] = self.m.request_digest(header)
        event.pop("record_digest")
        digest = self.m.request_digest(event)
        self.write(root / "event-0000.json", {**event, "record_digest": digest})
        self.write(
            root / "closed.json", dict(events=1, journal_digest=digest, complete=True)
        )

    def rebind(self, case):
        """Test-only protected re-attestation after intentional journal changes."""
        directory = self.bundle / case
        old = self.m.CausalSignedEvidence.model_validate_json(
            (directory / "fault.json").read_bytes()
        )
        journals = [
            self.m.read_journal(directory / f"host-{i}", self.challenge, self.registry)
            for i in range(2)
        ]
        binding = self.m.ProvenanceBinding(
            challenge_digest=self.m.request_digest(
                self.challenge.model_dump(mode="json")
            ),
            case_id=case,
            journals=[(h["host_identity"], d) for h, _, d in journals],
            process_headers=[self.m.request_digest(h) for h, _, _ in journals],
            fault_record_digest=self.m.request_digest((case, "fault")),
        )
        self.write(directory / "binding.json", binding)
        fault = old.evidence.payload.model_copy(
            update={
                "protected_record_digest": self.m.request_digest(
                    binding.model_dump(mode="json")
                )
            }
        )
        self.write(
            directory / "fault.json",
            self.sign(
                fault,
                "fault",
                start=old.evidence.started_at,
                end=old.evidence.completed_at,
            ),
        )
        sources = [r for r in self.sources(case) if r.evidence.payload.source != "host"]
        for i in range(2):
            draft = self.m.unsigned_draft(
                directory / f"host-{i}",
                self.challenge,
                self.registry,
                self.trust,
                sources,
                binding,
            )
            self.write(
                directory / f"host-{i}.json",
                self.sign(
                    draft["payload"],
                    "host",
                    i,
                    start=draft["started_at"],
                    end=draft["completed_at"],
                ),
            )
        self.review(case)
