"""Quota rows are locked last, within the owning AgentStore transaction.

Never locks or updates another session/attempt. Expiry is a DB-clock predicate,
not a sweep that takes foreign session locks while holding a provider lock.
"""

import hashlib
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from agentflow.application.ports.agent_store import StateConflict
from agentflow.application.ports.provider_admission import (
    MAX_COOLDOWN_SECONDS,
    ModelPermit,
    ProviderAdmissionPolicy,
)
from agentflow.infrastructure.persistence.models import (
    ProviderAdmissionRow,
    ProviderPermitRow,
)


def token_digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def permit_record(row):
    return ModelPermit.model_validate(
        {name: getattr(row, name) for name in ModelPermit.model_fields}
    )


@dataclass(frozen=True)
class AdmissionGrant:
    policy: ProviderAdmissionPolicy
    permit: ModelPermit
    token_digest: str


@dataclass(frozen=True)
class AdmissionDelay:
    available_at: float


class SQLProviderAdmissionStore:
    """Internal quota component: caller owns Run/Step/Session/Attempt locks."""

    def __init__(self, store):
        self.store = store

    def _policy(self, db, key):
        row = db.scalar(
            select(ProviderAdmissionRow)
            .where(ProviderAdmissionRow.provider_key == key)
            .with_for_update()
        )
        if row is None:
            raise StateConflict("provider admission policy is missing")
        return row

    def acquire(self, db, policy, task, state, candidate):
        insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
        # Concurrent first use must converge on exactly one immutable policy.
        db.execute(
            insert(ProviderAdmissionRow)
            .values(
                provider_key=policy.provider_key,
                policy=policy.model_dump(mode="json"),
                cooldown_until=0,
                created_at=self.store._now(db),
            )
            .on_conflict_do_nothing(index_elements=["provider_key"])
        )
        row = self._policy(db, policy.provider_key)
        if row.policy != policy.model_dump(mode="json"):
            raise StateConflict("provider admission policy differs across workers")
        now = self.store._now(db)  # Re-read after waiting for the shared row lock.
        window = ProviderPermitRow.created_at > now - policy.window_seconds
        total, oldest = db.execute(
            select(func.count(), func.min(ProviderPermitRow.created_at)).where(
                ProviderPermitRow.provider_key == policy.provider_key, window
            )
        ).one()
        active = db.scalar(
            select(func.count())
            .select_from(ProviderPermitRow)
            .where(
                ProviderPermitRow.provider_key == policy.provider_key,
                ProviderPermitRow.status.in_(["active", "unknown"]),
                ProviderPermitRow.expires_at > now,
            )
        )
        if (
            row.cooldown_until > now
            or total >= policy.max_requests_per_window
            or (active >= policy.max_concurrent_requests)
        ):
            return AdmissionDelay(
                max(
                    now + policy.admission_retry_delay_seconds,
                    row.cooldown_until,
                    oldest + policy.window_seconds
                    if total >= policy.max_requests_per_window
                    else 0,
                )
            )
        retry = state.snapshot.model_call_id == candidate.model_call_id
        index = state.model_calls[-1].attempts + 1 if retry else 1
        # Force a row version change: stronger PG isolation must serialize/abort,
        # not grant from an old permit snapshot after obtaining an unchanged lock.
        row.revision += 1
        return AdmissionGrant(
            policy=policy,
            token_digest=token_digest(task.token),
            permit=ModelPermit(
                id=str(uuid4()),
                provider_key=policy.provider_key,
                session_id=state.snapshot.id,
                model_call_id=candidate.model_call_id,
                attempt_index=index,
                reservation_revision=candidate.revision,
                status="active",
                created_at=now,
                expires_at=now + policy.permit_ttl_seconds,
            ),
        )

    def record(self, db, grant):
        db.add(
            ProviderPermitRow(
                **grant.permit.model_dump(),
                token_digest=grant.token_digest,
            )
        )

    def confirm(self, db, session, call, outcome, task):
        permit = db.scalar(
            select(ProviderPermitRow).where(
                ProviderPermitRow.session_id == session.id,
                ProviderPermitRow.model_call_id == call.model_call_id,
                ProviderPermitRow.attempt_index == call.attempts,
            )
        )
        if permit is None:
            if outcome.backpressure is not None and outcome.status != "failed":
                raise StateConflict("provider backpressure requires confirmed failure")
            return  # Pre-admission/legacy attempt; never retroactively issue a permit.
        if outcome.status != "unknown" and (
            task is None or permit.token_digest != token_digest(task.token)
        ):
            raise StateConflict("model result does not own the original permit")
        policy_row = self._policy(db, permit.provider_key)
        if permit.status not in {"active", "unknown"}:
            raise StateConflict("model permit is already released")
        now = self.store._now(db)
        policy_row.revision += 1
        if outcome.backpressure is not None:
            if outcome.status != "failed":
                raise StateConflict("provider backpressure requires confirmed failure")
            policy = ProviderAdmissionPolicy.model_validate(policy_row.policy)
            delay = min(
                MAX_COOLDOWN_SECONDS,
                max(
                    policy.admission_retry_delay_seconds,
                    outcome.backpressure.retry_after_seconds,
                ),
            )
            policy_row.cooldown_until = max(policy_row.cooldown_until, now + delay)
        if outcome.status == "unknown":
            permit.status = "unknown"  # Keep capacity until the original fixed TTL.
        else:
            if permit.status == "unknown":
                raise StateConflict("unknown request cannot be automatically confirmed")
            permit.status, permit.released_at = "released", now

    def validate(self, db, task, state, supplied, policy):
        if policy is None or state.provider_key != policy.provider_key:
            raise StateConflict("dispatch requires the bound deployment policy")
        row = self._policy(db, policy.provider_key)
        if row.policy != policy.model_dump(mode="json"):
            raise StateConflict("provider admission policy differs across workers")
        saved = db.get(ProviderPermitRow, supplied.id)
        if (
            saved is None
            or permit_record(saved) != supplied
            or saved.token_digest != token_digest(task.token)
            or saved.session_id != state.snapshot.id
            or saved.model_call_id != state.snapshot.model_call_id
            or state.snapshot.revision != saved.reservation_revision
            or state.snapshot.status != "model_inflight"
            or state.model_attempts[-1].outcome != "inflight"
            or saved.status != "active"
            or saved.expires_at <= self.store._now(db)
        ):
            raise StateConflict("model permit is stale or expired")
