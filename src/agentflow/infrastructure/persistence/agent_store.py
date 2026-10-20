"""复用 Workflow Store 的连接、锁和时钟；数据库不保证外部副作用 exactly-once。"""

import json
import math
import re
from contextlib import contextmanager
from uuid import uuid4

from pydantic import JsonValue, ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from agentflow.application.agents.context import AgentContextBuilder, UnresolvedActions
from agentflow.application.agents.projection import project_context
from agentflow.application.ports.agent_store import (
    ActionObservation,
    AgentDataError,
    AgentMessage,
    AgentSession,
    AgentStoreUnavailable,
    AgentTransition,
    LostLease,
    ModelAttemptRecord,
    ModelCallRecord,
    ModelCallResult,
    ObservedUsageBlocked,
    StateConflict,
    StoredAction,
)
from agentflow.application.ports.context_projection import (
    ContextProjectionError,
    ContextProjectionManifest,
    ContextProjectionPolicy,
)
from agentflow.application.ports.model_request import (
    ModelRequestContract,
    ModelRequestError,
    check_request_continuity,
)
from agentflow.application.ports.model_usage import (
    MAX_USAGE_BYTES,
    ModelUsage,
    ModelUsageSummary,
    UsageMappingRegistry,
)
from agentflow.application.ports.observed_usage import (
    ObservedUsagePolicy,
    UsageGovernanceBinding,
    assess_usage,
    stricter_policy,
)
from agentflow.application.ports.provider_admission import (
    ModelAdmission,
    ProviderAdmissionPolicy,
)
from agentflow.domain.agent.action import Action, digest_arguments
from agentflow.domain.agent.review import (
    ActionApproval,
    ActionReconciliation,
    ApprovalDecision,
    ReconciliationDecision,
    ToolBinding,
)
from agentflow.domain.agent.run import AgentSnapshot
from agentflow.domain.agent.status import ActionStatus, AgentStatus
from agentflow.domain.execution.exception import InvalidStateTransitionError
from agentflow.domain.execution.task import Task
from agentflow.domain.workflow.spec import WorkflowSpec
from agentflow.infrastructure.persistence.models import (
    AgentActionApprovalRow,
    AgentActionReconciliationRow,
    AgentActionRow,
    AgentMessageRow,
    AgentModelAttemptRow,
    AgentModelCallRow,
    AgentSessionRow,
    ProviderAdmissionRow,
    ProviderPermitRow,
    RunRow,
    StepRow,
)
from agentflow.infrastructure.persistence.provider_admission import (
    AdmissionDelay,
    AdmissionGrant,
    SQLProviderAdmissionStore,
    permit_record,
)
from agentflow.infrastructure.persistence.store import NotFoundError, Store, json_bytes

MAX_MESSAGE_BYTES = 100_000
MAX_ARGUMENT_BYTES = 100_000
MAX_RESULT_BYTES = 100_000
MAX_SESSION_BYTES = 8_000_000
# Covers bounded actor/reason/evidence, identifiers and JSON escaping/record keys.
REVIEW_METADATA_RESERVE_BYTES = 20_000


def checked_json(value: JsonValue, limit: int = 2_000_000) -> JsonValue:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, allow_nan=False
        ).encode()
    except (TypeError, ValueError):
        raise AgentDataError("agent payload must be finite JSON") from None
    if len(encoded) > limit:
        raise AgentDataError("agent payload exceeds storage limit")
    return json.loads(encoded)


def action_key(session_id: str, action_id: str) -> str:
    return "agent:" + digest_arguments(
        {"session_id": session_id, "action_id": action_id}
    )


class SQLAgentStore:
    def __init__(
        self,
        store: Store,
        *,
        admission_policy: ProviderAdmissionPolicy | None = None,
        observed_usage_policy: ObservedUsagePolicy | None = None,
        usage_mappings: UsageMappingRegistry | None = None,
    ) -> None:
        self.store = store
        self.admission_policy = (
            ProviderAdmissionPolicy.model_validate(admission_policy.model_dump())
            if admission_policy is not None
            else None
        )
        self.admission = SQLProviderAdmissionStore(store)
        self.observed_usage_policy = (
            ObservedUsagePolicy.model_validate(observed_usage_policy.model_dump())
            if observed_usage_policy is not None
            else None
        )
        self.usage_mappings = usage_mappings or UsageMappingRegistry()

    def _usage_policy(self, db, row, *, check_deployment=False):
        step = db.get(StepRow, row.step_row_id)
        run = db.get(RunRow, step.run_id)
        binding = UsageGovernanceBinding.model_validate(run.usage_governance or {})
        if check_deployment and (
            binding.deployment_policy != self.observed_usage_policy
            or (
                binding.usage_mappings is not None
                and binding.usage_mappings != self.usage_mappings.definitions
            )
        ):
            raise StateConflict("observed usage configuration differs from Run binding")
        spec = next(
            s for s in WorkflowSpec.model_validate(run.spec).steps if s.id == step.key
        )
        raw = spec.parameters.get("agent", {}).get("observed_usage")
        workflow = ObservedUsagePolicy.model_validate(raw) if raw is not None else None
        return stricter_policy(binding.deployment_policy, workflow)

    def _check_observed_usage(self, db, row, state):
        self._usage_policy(db, row, check_deployment=True)
        if state.observed_usage is not None and state.observed_usage.blocked:
            raise ObservedUsageBlocked(state.observed_usage)

    @contextmanager
    def _owned_transaction(self, task):
        try:
            with self.store.transaction() as db:
                try:
                    owned = self.store._owned(db, task)
                except NotFoundError:
                    owned = None
                if owned is None:
                    raise LostLease("task does not own an active lease")
                run, step = owned
                deadline = step.lease_until
                yield db, run, step
                # 包括成功/挂起释放 token 的事务，也须在原租约到期前提交。
                db.flush()
                if deadline <= self.store._now(db):
                    raise LostLease("task lease expired before commit")
        except IntegrityError:
            # SQLAlchemy 的原始异常可能携带参数正文；不将其作为异常链输出。
            raise StateConflict(
                "agent record uniqueness or reference conflict"
            ) from None
        except SQLAlchemyError:
            raise AgentStoreUnavailable(
                "agent persistence unavailable; reload before dispatch"
            ) from None

    def _session(self, db, step):
        return db.scalar(
            select(AgentSessionRow)
            .where(
                AgentSessionRow.step_row_id == step.id,
            )
            .with_for_update()
        )

    def _read(self, db, row, *, reserve_capacity=False):
        try:
            # Run/Step/Session 已持锁；审批先于模型和 Action 获取锁。
            approval_rows = list(
                db.scalars(
                    select(AgentActionApprovalRow)
                    .where(AgentActionApprovalRow.session_id == row.id)
                    .order_by(AgentActionApprovalRow.id)
                    .with_for_update()
                )
            )
            snapshot = AgentSnapshot.model_validate(checked_json(row.snapshot))
            initial = AgentSnapshot.model_validate(checked_json(row.initial_snapshot))
            if (
                row.revision != snapshot.revision
                or row.id != snapshot.id
                or initial.id != row.id
            ):
                raise StateConflict("stored snapshot revision or identity mismatch")
            messages = tuple(
                AgentMessage(
                    sequence=m.sequence,
                    role=m.role,
                    payload=checked_json(m.payload, MAX_MESSAGE_BYTES),
                    model_call_id=m.model_call_id,
                )
                for m in db.scalars(
                    select(AgentMessageRow)
                    .where(
                        AgentMessageRow.session_id == row.id,
                    )
                    .order_by(AgentMessageRow.sequence)
                )
            )
            calls = tuple(
                ModelCallRecord(
                    model_call_id=c.model_call_id,
                    round_index=c.round_index,
                    status=c.status,
                    attempts=c.attempts,
                    usage=checked_json(c.usage, MAX_MESSAGE_BYTES),
                )
                for c in db.scalars(
                    select(AgentModelCallRow)
                    .where(
                        AgentModelCallRow.session_id == row.id,
                    )
                    .order_by(AgentModelCallRow.round_index)
                    .with_for_update()
                )
            )
            call_order = {call.model_call_id: call.round_index for call in calls}
            attempts = [
                ModelAttemptRecord(
                    session_id=a.session_id,
                    model_call_id=a.model_call_id,
                    attempt_index=a.attempt_index,
                    outcome=a.outcome,
                    accounting=a.accounting,
                    model=a.model,
                    request_id=a.request_id,
                    raw_usage=checked_json(a.raw_usage, MAX_USAGE_BYTES),
                    usage=ModelUsage.model_validate(
                        checked_json(a.usage, MAX_USAGE_BYTES)
                    ),
                    created_at=a.created_at,
                    confirmed_at=a.confirmed_at,
                    context_projection=ContextProjectionManifest.model_validate(
                        checked_json(a.context_projection, 4096)
                    )
                    if a.context_projection is not None
                    else None,
                    request_contract=ModelRequestContract.model_validate(
                        checked_json(a.request_contract, 8000)
                    )
                    if a.request_contract is not None
                    else None,
                )
                for a in db.scalars(
                    select(AgentModelAttemptRow)
                    .where(AgentModelAttemptRow.session_id == row.id)
                    .order_by(
                        AgentModelAttemptRow.model_call_id,
                        AgentModelAttemptRow.attempt_index,
                    )
                    .with_for_update()
                )
            ]
            if any(a.model_call_id not in call_order for a in attempts):
                raise StateConflict("attempt references an unknown model call")
            attempts.sort(key=lambda a: (call_order[a.model_call_id], a.attempt_index))
            # Unversioned historical numbers remain untouched. Versioned facts
            # are checked against persisted rules, not today's registry.
            # Cancellation/recovery must not need a live provider registration.
            for attempt in attempts:
                if attempt.usage.mapping is not None:
                    if (
                        attempt.usage.mapping.normalize(attempt.raw_usage)
                        != attempt.usage
                    ):
                        raise StateConflict(
                            "stored usage mapping is inconsistent"
                        ) from None
            permits = tuple(
                permit_record(p)
                for p in db.scalars(
                    select(ProviderPermitRow)
                    .where(ProviderPermitRow.session_id == row.id)
                    .order_by(
                        ProviderPermitRow.reservation_revision, ProviderPermitRow.id
                    )
                )
            )
            actions = []
            for a in db.scalars(
                select(AgentActionRow)
                .where(
                    AgentActionRow.session_id == row.id,
                )
                .order_by(AgentActionRow.model_call_id, AgentActionRow.call_index)
                .with_for_update()
            ):
                arguments = checked_json(a.arguments, MAX_ARGUMENT_BYTES)
                if digest_arguments(
                    arguments
                ) != a.arguments_digest or a.idempotency_key != action_key(
                    row.id, a.id
                ):
                    raise StateConflict("stored action digest or key mismatch")
                if a.model_call_id not in call_order:
                    raise StateConflict(
                        "stored action references an unknown model call"
                    )
                actions.append(
                    StoredAction(
                        action=Action(
                            id=a.id,
                            agent_id=row.id,
                            model_call_id=a.model_call_id,
                            tool_name=a.tool_name,
                            arguments_digest=a.arguments_digest,
                            side_effect=a.side_effect,
                            status=a.status,
                            error_code=a.error_code,
                        ),
                        call_index=a.call_index,
                        arguments=arguments,
                        idempotency_key=a.idempotency_key,
                        result=checked_json(a.result, MAX_RESULT_BYTES),
                        tool_binding=ToolBinding.model_validate(a.tool_binding)
                        if a.tool_binding is not None
                        else None,
                    )
                )
            actions.sort(
                key=lambda a: (call_order[a.action.model_call_id], a.call_index)
            )
            if any(
                a.tool_binding is not None and not a.tool_binding.matches(a.action)
                for a in actions
            ):
                raise StateConflict("stored tool binding differs from action")
            state = AgentSession(
                snapshot=snapshot,
                messages=messages,
                model_calls=calls,
                model_attempts=tuple(attempts),
                provider_key=row.provider_key,
                model_permits=permits,
                observed_usage=assess_usage(attempts, policy)
                if (policy := self._usage_policy(db, row)) is not None
                else None,
                actions=tuple(actions),
                approvals=tuple(self._approval(a, actions) for a in approval_rows),
                reconciliations=tuple(
                    self._reconciliation(r)
                    for r in db.scalars(
                        select(AgentActionReconciliationRow)
                        .where(AgentActionReconciliationRow.session_id == row.id)
                        .order_by(
                            AgentActionReconciliationRow.created_at,
                            AgentActionReconciliationRow.id,
                        )
                    )
                ),
            )
            self._validate_history(state)
            self._check_capacity(initial, state, reserve=reserve_capacity)
            return state
        except (ValidationError, KeyError, TypeError):
            raise StateConflict("stored agent data is inconsistent") from None

    def _approval(self, row, actions):
        stored = next((a for a in actions if a.action.id == row.action_id), None)
        if (
            stored is None
            or stored.tool_binding is None
            or row.tool_name != stored.tool_binding.tool_name
            or row.tool_version != stored.tool_binding.tool_version
            or row.tool_policy_digest != stored.tool_binding.tool_policy_digest
            or row.arguments_digest != stored.action.arguments_digest
        ):
            raise StateConflict("approval binding differs from stored action")
        return ActionApproval(
            approval_id=row.id,
            session_id=row.session_id,
            action_id=row.action_id,
            binding=stored.tool_binding,
            arguments_digest=row.arguments_digest,
            decision=row.decision,
            actor=row.actor,
            reason=row.reason,
            created_at=row.created_at,
            decided_at=row.decided_at,
            consumed_at=row.consumed_at,
        )

    def _reconciliation(self, row):
        return ActionReconciliation(
            request_id=row.id,
            session_id=row.session_id,
            action_id=row.action_id,
            expected_revision=row.request_revision,
            previous_status=row.previous_status,
            confirmed_status=row.confirmed_status,
            result=checked_json(row.result, MAX_RESULT_BYTES),
            error_code=row.error_code,
            evidence_reference=row.evidence_reference,
            actor=row.actor,
            reason=row.reason,
            applied=row.applied,
            created_at=row.created_at,
        )

    def _check_capacity(self, initial, state, *, reserve=False):
        data = {
            "initial": initial.model_dump(mode="json"),
            "state": state.model_dump(mode="json"),
        }
        # Backfilled metadata must not make previously valid legacy sessions
        # unreadable. All newly reserved attempts count toward the session quota.
        data["state"]["model_attempts"] = [
            a for a in data["state"]["model_attempts"] if a["accounting"] == "recorded"
        ]
        if not data["state"]["model_attempts"]:
            del data["state"]["model_attempts"]
        if not data["state"]["model_permits"]:
            del data["state"]["model_permits"]
        if data["state"]["provider_key"] is None:
            del data["state"]["provider_key"]
        if data["state"]["observed_usage"] is None:
            del data["state"]["observed_usage"]
        # Do not charge historical rows for a default-only JSON schema field.
        for attempt in data["state"].get("model_attempts", []):
            if attempt["usage"]["mapping"] is None:
                del attempt["usage"]["mapping"]
            if attempt["context_projection"] is None:
                del attempt["context_projection"]
            if attempt["request_contract"] is None:
                del attempt["request_contract"]
        checked_json(data, MAX_SESSION_BYTES)
        if not reserve or state.snapshot.status.is_terminal:
            return
        # 这是容量估算，不会写入数据库。派发前为所有未知结果及其观察预留空间，
        # 避免历史填满后无法取消/记录崩溃。使用最长错误码，并为版本增长留余量。
        projected = data["state"]
        snapshot = projected["snapshot"]
        snapshot["status"] = AgentStatus.CANCELLED.value
        snapshot["revision"] += 1
        error = "E" * 80
        for action in snapshot["actions"]:
            if action["status"] == ActionStatus.INFLIGHT.value:
                action["status"], action["error_code"] = "unknown", error
        for stored in projected["actions"]:
            action = stored["action"]
            if action["status"] != ActionStatus.INFLIGHT.value:
                continue
            action["status"], action["error_code"], stored["result"] = (
                "unknown",
                error,
                None,
            )
            projected["messages"].append(
                AgentMessage(
                    sequence=len(projected["messages"]),
                    role="tool",
                    model_call_id=action["model_call_id"],
                    payload={
                        "action_id": action["id"],
                        "status": "unknown",
                        "error_code": error,
                        "result": None,
                    },
                ).model_dump(mode="json")
            )
        for call in projected["model_calls"]:
            if call["status"] == "inflight":
                call["status"] = "unknown"
        pending_attempts = sum(a.outcome == "inflight" for a in state.model_attempts)
        AgentSnapshot.model_validate(snapshot)
        checked_json(snapshot, 2_000_000 - 1024)
        data["control_margin"] = "x" * 1024
        # Latest-call raw usage, attempt raw usage and normalized integers can
        # each grow by the bounded usage size. Admission reserves result space.
        remaining = pending_attempts * (3 * MAX_USAGE_BYTES + 4096)
        approvals = {a.action_id: a for a in state.approvals}
        for stored in state.actions:
            if stored.action.status not in {
                ActionStatus.PROPOSED,
                ActionStatus.INFLIGHT,
                ActionStatus.UNKNOWN,
            }:
                continue
            # A resolved UNKNOWN may store its full result three times: the
            # Action, appended observation and immutable reconciliation evidence.
            remaining += 3 * MAX_RESULT_BYTES + REVIEW_METADATA_RESERVE_BYTES
            if (
                stored.action.status == ActionStatus.PROPOSED
                and stored.tool_binding is not None
                and stored.tool_binding.protected
            ):
                approval = approvals.get(stored.action.id)
                if approval is None:
                    remaining += REVIEW_METADATA_RESERVE_BYTES
                elif approval.decision == "pending":
                    remaining += max(
                        0,
                        REVIEW_METADATA_RESERVE_BYTES
                        - len(json_bytes(approval.model_dump(mode="json"))),
                    )
        checked_json(data, MAX_SESSION_BYTES - remaining)

    def _validate_history(self, state):
        snapshot, calls, messages = state.snapshot, state.model_calls, state.messages
        if [m.sequence for m in messages] != list(range(len(messages))):
            raise StateConflict("stored message sequence is not contiguous")
        if len(calls) != snapshot.budget.model_rounds or any(
            c.round_index != index for index, c in enumerate(calls, 1)
        ):
            raise StateConflict("model history does not match model round budget")
        if sum(c.attempts for c in calls) != snapshot.budget.model_attempts:
            raise StateConflict("model history does not match attempt budget")
        if len(state.model_attempts) != snapshot.budget.model_attempts:
            raise StateConflict("attempt ledger does not match attempt budget")
        attempts_by_id = {
            (a.model_call_id, a.attempt_index): a for a in state.model_attempts
        }
        for permit in state.model_permits:
            attempt = attempts_by_id.get((permit.model_call_id, permit.attempt_index))
            if (
                permit.session_id != snapshot.id
                or permit.provider_key != state.provider_key
                or attempt is None
                or permit.reservation_revision > snapshot.revision
                or permit.status
                != {
                    "inflight": "active",
                    "unknown": "unknown",
                    "failed": "released",
                    "succeeded": "released",
                }.get(attempt.outcome)
                or (permit.status == "released") != (permit.released_at is not None)
            ):
                raise StateConflict("provider permit differs from model attempt")
        if (
            state.provider_key is not None
            and snapshot.status == AgentStatus.MODEL_INFLIGHT
        ):
            latest = state.model_attempts[-1]
            if not any(
                p.model_call_id == latest.model_call_id
                and p.attempt_index == latest.attempt_index
                for p in state.model_permits
            ):
                raise StateConflict("admission-bound model attempt requires a permit")
        for call in calls:
            attempts = [
                a for a in state.model_attempts if a.model_call_id == call.model_call_id
            ]
            if (
                [a.attempt_index for a in attempts] != list(range(1, call.attempts + 1))
                or any(a.session_id != snapshot.id for a in attempts)
                or attempts[-1].outcome != call.status
                or any(
                    a.outcome not in {"failed", "LEGACY_UNMEASURED"}
                    for a in attempts[:-1]
                )
                or (
                    attempts[-1].accounting == "recorded"
                    and attempts[-1].raw_usage != call.usage
                )
            ):
                raise StateConflict("attempt ledger differs from logical model call")
            if any(
                a.context_projection != attempts[0].context_projection
                for a in attempts[1:]
            ):
                raise StateConflict("logical model retry context changed")
            if any(
                a.request_contract != attempts[0].request_contract for a in attempts[1:]
            ):
                raise StateConflict("logical model retry request changed")
        profiles = {
            a.request_contract.profile_digest()
            for a in state.model_attempts
            if a.request_contract is not None
        }
        if len(profiles) > 1:
            raise StateConflict("agent session model profile changed")
        for attempt in state.model_attempts:
            if attempt.request_contract is not None:
                if attempt.context_projection is None:
                    raise StateConflict("model request lacks a projection identity")
                try:
                    attempt.request_contract.verify_projection(
                        attempt.context_projection
                    )
                except ModelRequestError:
                    raise StateConflict(
                        "model request differs from projection"
                    ) from None
        if calls and calls[-1].model_call_id != snapshot.model_call_id:
            raise StateConflict("snapshot does not reference the latest model call")
        if (
            sum(
                a.action.status not in {ActionStatus.PROPOSED, ActionStatus.DENIED}
                for a in state.actions
            )
            != snapshot.budget.tool_calls
        ):
            raise StateConflict("action history does not match tool budget")
        current = tuple(
            a.action
            for a in state.actions
            if a.action.model_call_id == snapshot.model_call_id
        )
        if current != snapshot.actions:
            raise StateConflict("current action batch differs from snapshot")
        by_id = {a.action.id: a for a in state.actions}
        observations = {}
        for message in messages:
            if message.role in {"system", "user"}:
                if message.model_call_id is not None:
                    raise StateConflict("context messages cannot reference model calls")
            elif message.role == "tool":
                payload = message.payload
                stored = by_id.get(payload.get("action_id"))
                if (
                    stored is None
                    or stored.action.model_call_id != message.model_call_id
                ):
                    raise StateConflict("tool observation references an unknown action")
                if payload.get("status") not in {
                    "succeeded",
                    "failed",
                    "unknown",
                    "denied",
                }:
                    raise StateConflict("tool observation requires an action outcome")
                Action.model_validate(
                    {
                        **stored.action.model_dump(),
                        "status": payload.get("status"),
                        "error_code": payload.get("error_code"),
                    }
                )
                if set(payload) != {"action_id", "status", "error_code", "result"}:
                    raise StateConflict("invalid tool observation shape")
                observations[stored.action.id] = payload
            elif message.model_call_id not in {c.model_call_id for c in calls}:
                raise StateConflict(
                    "assistant message references an unknown model call"
                )
        for call in calls:
            batch = [
                a for a in state.actions if a.action.model_call_id == call.model_call_id
            ]
            if [a.call_index for a in batch] != list(range(len(batch))):
                raise StateConflict("stored action indices are not contiguous")
            replies = [
                m
                for m in messages
                if m.role == "assistant" and m.model_call_id == call.model_call_id
            ]
            if len(replies) != int(call.status == "succeeded"):
                raise StateConflict("model result and assistant message disagree")
            if replies and replies[0].payload.get("action_ids", []) != [
                a.action.id for a in batch
            ]:
                raise StateConflict("assistant message and action order disagree")
        for a in state.actions:
            if a.action.status not in {ActionStatus.PROPOSED, ActionStatus.INFLIGHT}:
                expected = {
                    "action_id": a.action.id,
                    "status": a.action.status.value,
                    "error_code": a.action.error_code,
                    "result": a.result,
                }
                if observations.get(a.action.id) != expected:
                    raise StateConflict(
                        "action outcome and latest observation disagree"
                    )
        if calls:
            status = calls[-1].status
            if snapshot.status == AgentStatus.MODEL_INFLIGHT and status not in {
                "inflight",
                "unknown",
            }:
                raise StateConflict("model inflight snapshot has no pending request")
            if snapshot.status == AgentStatus.READY_MODEL and status not in {
                "succeeded",
                "failed",
            }:
                raise StateConflict("ready model requires a confirmed previous outcome")
            if (
                snapshot.status
                in {
                    AgentStatus.ACTIONS_PENDING,
                    AgentStatus.WAITING_APPROVAL,
                    AgentStatus.WAITING_RECONCILIATION,
                    AgentStatus.SUCCEEDED,
                }
                and status != "succeeded"
            ):
                raise StateConflict("action or success state requires a model response")
            if snapshot.status.is_terminal and status == "inflight":
                raise StateConflict(
                    "terminal agent cannot retain an inflight model call"
                )

    def _append(self, db, row, messages, next_sequence, now):
        for index, message in enumerate(
            sorted(messages, key=lambda m: m.sequence), next_sequence
        ):
            if message.sequence != index:
                raise StateConflict("message sequence must append contiguously")
            db.add(
                AgentMessageRow(
                    id=str(uuid4()),
                    session_id=row.id,
                    sequence=message.sequence,
                    role=message.role,
                    payload=checked_json(message.payload, MAX_MESSAGE_BYTES),
                    model_call_id=message.model_call_id,
                    created_at=now,
                )
            )

    def open(
        self,
        task: Task,
        initial_snapshot: AgentSnapshot,
        *,
        messages: tuple[AgentMessage, ...] = (),
    ) -> AgentSession:
        initial_snapshot = AgentSnapshot.model_validate(
            checked_json(initial_snapshot.model_dump(mode="json"))
        )
        if (
            initial_snapshot.status != AgentStatus.READY_MODEL
            or initial_snapshot.revision != 0
            or initial_snapshot.actions
            or initial_snapshot.model_call_id is not None
            or any(
                getattr(initial_snapshot.budget, r)
                for r in (
                    "model_rounds",
                    "model_attempts",
                    "tool_calls",
                    "resource_units",
                )
            )
        ):
            raise StateConflict("initial agent snapshot must be unused and ready")
        if any(
            m.role not in {"user", "system"} or m.model_call_id is not None
            for m in messages
        ):
            raise StateConflict("initial messages must be context messages")
        data = initial_snapshot.model_dump(mode="json")
        with self._owned_transaction(task) as (db, run, step):
            if run.usage_governance is None:
                run.usage_governance = UsageGovernanceBinding(
                    deployment_policy=self.observed_usage_policy,
                    usage_mappings=self.usage_mappings.definitions,
                ).model_dump(mode="json")
            row = self._session(db, step)
            created = row is None
            if row is None:
                now = self.store._now(db)
                row = AgentSessionRow(
                    id=initial_snapshot.id,
                    step_row_id=step.id,
                    initial_snapshot=data,
                    snapshot=data,
                    revision=0,
                    provider_key=self.admission_policy.provider_key
                    if self.admission_policy
                    else None,
                    created_at=now,
                    updated_at=now,
                )
                db.add(row)
                db.flush()
                self._append(db, row, messages, 0, now)
                db.flush()
                self.store._event(
                    db, run.id, "agent.created", {"step": step.key, "revision": 0}
                )
            elif row.initial_snapshot != data:
                raise StateConflict("step already has a different agent initialization")
            self._usage_policy(db, row, check_deployment=True)
            result = self._read(db, row, reserve_capacity=created)
            if result.messages[: len(messages)] != tuple(messages):
                raise StateConflict("initial messages differ from stored history")
        return result

    def load(self, task: Task) -> AgentSession:
        with self._owned_transaction(task) as (db, _run, step):
            row = self._session(db, step)
            if row is None:
                raise StateConflict("agent session is not initialized")
            result = self._read(db, row)
        return result

    def usage_summary(self, session_id: str) -> ModelUsageSummary:
        with self._operator_transaction(session_id) as (_db, _run, _step, _row, state):
            return state.usage_summary()

    def reserve_model_attempt(
        self, task: Task, transition: AgentTransition
    ) -> ModelAdmission:
        if self.admission_policy is None:
            raise StateConflict("model admission requires a deployment policy")
        with self._owned_transaction(task) as (db, run, step):
            row = self._session(db, step)
            if row is None:
                raise StateConflict("agent session is not initialized")
            state = self._read(db, row)
            old, candidate = state.snapshot, transition.snapshot
            try:
                old.advance(candidate)
            except (ValueError, InvalidStateTransitionError):
                raise StateConflict(
                    "invalid model admission revision or transition"
                ) from None
            if (
                old.status != AgentStatus.READY_MODEL
                or candidate.status != AgentStatus.MODEL_INFLIGHT
                or transition.model_result is not None
                or transition.messages
                or transition.arguments
                or transition.observations
                or transition.authorizations
                or transition.error_code is not None
                or row.provider_key not in {None, self.admission_policy.provider_key}
            ):
                raise StateConflict(
                    "invalid model admission reservation or provider binding"
                )
            self._validate_projection(run, step, state, transition)
            row.provider_key = self.admission_policy.provider_key
            self._check_observed_usage(db, row, state)
            grant = self.admission.acquire(
                db, self.admission_policy, task, state, candidate
            )
            if isinstance(grant, AdmissionDelay):
                waiting = AgentSnapshot.model_validate(
                    {
                        **old.model_dump(),
                        "revision": old.revision + 1,
                    }
                )
                result = self._apply(
                    db, run, step, row, AgentTransition(snapshot=waiting), task=task
                )
                row.continuation_pending = True
                step.status, step.error = "ready", "ProviderAdmissionDeferred"
                step.available_at = grant.available_at
                step.token, step.lease_until = None, None
                self.store._event(
                    db,
                    run.id,
                    "agent.admission_deferred",
                    {
                        "step": step.key,
                        "revision": waiting.revision,
                        "available_at": grant.available_at,
                        "error_code": "ProviderAdmissionDeferred",
                    },
                )
                answer = ModelAdmission(result, "admission_deferred")
            else:
                result = self._apply(
                    db, run, step, row, transition, task=task, grant=grant
                )
                answer = ModelAdmission(result, "reserved", grant.permit)
        return answer

    def validate_model_dispatch(self, task: Task, permit) -> None:
        with self._owned_transaction(task) as (db, _run, step):
            row = self._session(db, step)
            if row is None:
                raise StateConflict("agent session is not initialized")
            self.admission.validate(
                db, task, self._read(db, row), permit, self.admission_policy
            )

    def _validate_projection(self, run, step, state, transition):
        request = transition.context_projection
        contract = transition.request_contract
        starts = (
            state.snapshot.status == AgentStatus.READY_MODEL
            and transition.snapshot.status == AgentStatus.MODEL_INFLIGHT
        )
        if not starts:
            if request is not None or contract is not None:
                raise StateConflict(
                    "context manifest belongs to model reservation only"
                )
            return
        spec = next(
            s for s in WorkflowSpec.model_validate(run.spec).steps if s.id == step.key
        )
        config = spec.parameters.get("agent", {})
        retry = bool(state.model_calls and state.model_calls[-1].status == "failed")
        previous = state.model_attempts[-1].context_projection if retry else None
        # Preserve older direct Store clients. Explicit projection configuration
        # and calls already bound to a manifest must never bypass this check.
        if request is None:
            if (
                "context_projection" in config
                or previous is not None
                or contract is not None
            ):
                raise ContextProjectionError()
            if retry or any(
                a.request_contract is not None for a in state.model_attempts
            ):
                raise ModelRequestError("ModelRequestContractUnavailable")
            return
        try:
            full = AgentContextBuilder().build(state)
            projected = project_context(
                full,
                list(request.tools),
                ContextProjectionPolicy.model_validate(
                    config.get("context_projection", {})
                ),
                config.get("max_context_bytes", 1_000_000),
            )
            expected = projected.manifest
        except (ValidationError, UnresolvedActions):
            raise ContextProjectionError() from None
        if expected != request.manifest or (retry and expected != previous):
            raise ContextProjectionError("AgentContextProjectionChanged")
        if retry and (
            contract is None or state.model_attempts[-1].request_contract is None
        ):
            raise ModelRequestError("ModelRequestContractUnavailable")
        if contract is not None:
            contract.verify_projection(expected)
            contract.verify_context(projected.messages, list(request.tools))
            check_request_continuity(state.model_attempts, contract, retry=retry)
        elif any(a.request_contract is not None for a in state.model_attempts):
            raise ModelRequestError("ModelRequestContractUnavailable")

    def _apply(
        self, db, run, step, row, transition, *, operator=False, task=None, grant=None
    ):
        try:
            data = transition.model_dump(mode="json")
            projection = data.pop("context_projection")
            transition = AgentTransition.model_validate(
                {**checked_json(data), "context_projection": projection}
            )
        except ValidationError:
            raise AgentDataError("invalid agent transition data") from None
        old_state = self._read(db, row)
        old, candidate = old_state.snapshot, transition.snapshot
        try:
            old.advance(candidate)
        except (ValueError, InvalidStateTransitionError):
            raise StateConflict("invalid agent revision or state transition") from None
        self._validate_projection(run, step, old_state, transition)
        if (
            old.status == AgentStatus.READY_MODEL
            and candidate.status == AgentStatus.MODEL_INFLIGHT
        ):
            self._check_observed_usage(db, row, old_state)
        now = self.store._now(db)
        supplied = set(transition.authorizations)
        required = set()
        stored_actions = {a.action.id: a for a in old_state.actions}
        for previous, action in zip(old.actions, candidate.actions, strict=False):
            if (
                previous.status == ActionStatus.UNKNOWN
                and previous != action
                and not operator
            ):
                raise StateConflict("reconciliation requires an operator transaction")
            if (
                previous.status != ActionStatus.PROPOSED
                or action.status != ActionStatus.INFLIGHT
            ):
                continue
            stored = stored_actions[previous.id]
            binding = stored.tool_binding
            if binding is None or not binding.matches(previous):
                raise StateConflict("dispatch requires a verifiable tool binding")
            if binding.protected:
                approval = next(
                    (a for a in old_state.approvals if a.action_id == action.id), None
                )
                if (
                    approval is None
                    or not approval.authorizes(previous, binding)
                    or approval.consumed_at is not None
                    or approval.approval_id not in supplied
                ):
                    raise StateConflict(
                        "protected dispatch requires unused action approval"
                    )
                required.add(approval.approval_id)
                db.get(AgentActionApprovalRow, approval.approval_id).consumed_at = now
        if len(supplied) != len(transition.authorizations) or supplied != required:
            raise StateConflict(
                "authorizations must match protected dispatches exactly"
            )
        changed = db.execute(
            update(AgentSessionRow)
            .where(
                AgentSessionRow.id == row.id,
                AgentSessionRow.revision == old.revision,
            )
            .values(
                snapshot=checked_json(candidate.model_dump(mode="json")),
                revision=candidate.revision,
                updated_at=now,
            )
            .execution_options(synchronize_session="fetch")
        )
        if changed.rowcount != 1:
            raise StateConflict("agent revision changed concurrently")
        self._model_update(db, row, old, transition, now, task=task, grant=grant)
        dispatched = candidate.budget.tool_calls - old.budget.tool_calls
        if dispatched:
            self.store._metric(db, "agent.action_reserved", {"count": dispatched})
        messages = list(transition.messages)
        if any(m.role == "tool" for m in messages):
            raise StateConflict("tool messages must be committed as observations")
        new_batch = (
            old.status == AgentStatus.MODEL_INFLIGHT
            and candidate.status == AgentStatus.ACTIONS_PENDING
        )
        arguments = {a.action_id: a.arguments for a in transition.arguments}
        bindings = {a.action_id: a.tool_binding for a in transition.arguments}
        if len(arguments) != len(transition.arguments) or set(arguments) != (
            {a.id for a in candidate.actions} if new_batch else set()
        ):
            raise StateConflict(
                "action parameters must match the proposed batch exactly"
            )
        previous = {a.id: a for a in old.actions}
        observations = {o.action_id: o for o in transition.observations}
        if len(observations) != len(transition.observations):
            raise StateConflict("duplicate action observations")
        expected_observations = set()
        db.flush()  # 模型预约必须先存在，消息/Action 的复合外键才可引用它。
        for index, action in enumerate(candidate.actions):
            if new_batch:
                values = checked_json(arguments[action.id], MAX_ARGUMENT_BYTES)
                binding = bindings[action.id]
                if binding is not None and not binding.matches(action):
                    raise StateConflict("tool binding differs from action metadata")
                if digest_arguments(values) != action.arguments_digest:
                    raise AgentDataError("action arguments digest mismatch")
                db.add(
                    AgentActionRow(
                        session_id=row.id,
                        id=action.id,
                        model_call_id=action.model_call_id,
                        call_index=index,
                        tool_name=action.tool_name,
                        arguments_digest=action.arguments_digest,
                        arguments=values,
                        side_effect=action.side_effect.value,
                        status=action.status.value,
                        idempotency_key=action_key(row.id, action.id),
                        result=None,
                        error_code=action.error_code,
                        created_at=now,
                        updated_at=now,
                        tool_binding=binding.model_dump(mode="json")
                        if binding
                        else None,
                    )
                )
            elif previous.get(action.id) != action:
                saved = db.get(AgentActionRow, (row.id, action.id))
                if saved is None:
                    raise StateConflict("action history is missing")
                saved.status, saved.error_code, saved.updated_at = (
                    action.status.value,
                    action.error_code,
                    now,
                )
                if action.status != ActionStatus.INFLIGHT:
                    expected_observations.add(action.id)
                    observation = observations.get(action.id)
                    if observation is None:
                        raise StateConflict("action outcome requires an observation")
                    saved.result = checked_json(observation.result, MAX_RESULT_BYTES)
                    messages.append(
                        AgentMessage(
                            sequence=observation.sequence,
                            role="tool",
                            model_call_id=action.model_call_id,
                            payload={
                                "action_id": action.id,
                                "status": action.status.value,
                                "error_code": action.error_code,
                                "result": saved.result,
                            },
                        )
                    )
        if set(observations) != expected_observations:
            raise StateConflict("observations must match changed action outcomes")
        self._append(db, row, messages, len(old_state.messages), now)
        self.store._event(
            db,
            run.id,
            "agent.transition",
            {
                "step": step.key,
                "state": candidate.status.value,
                "revision": candidate.revision,
                "error_code": transition.error_code,
            },
        )
        db.flush()
        db.expire(row)
        return self._read(db, row, reserve_capacity=not operator)

    def _model_update(self, db, row, old, transition, now, *, task=None, grant=None):
        candidate, outcome = transition.snapshot, transition.model_result
        starts = (
            old.status == AgentStatus.READY_MODEL
            and candidate.status == AgentStatus.MODEL_INFLIGHT
        )
        call = db.scalar(
            select(AgentModelCallRow)
            .where(
                AgentModelCallRow.session_id == row.id,
                AgentModelCallRow.model_call_id == candidate.model_call_id,
            )
            .with_for_update()
        )
        if starts:
            if row.provider_key is not None or self.admission_policy is not None:
                if (
                    not isinstance(grant, AdmissionGrant)
                    or grant.policy != self.admission_policy
                    or grant.permit.session_id != row.id
                    or grant.permit.model_call_id != candidate.model_call_id
                    or grant.permit.reservation_revision != candidate.revision
                    or grant.permit.provider_key != row.provider_key
                    or grant.permit.expires_at <= now
                ):
                    raise StateConflict(
                        "model reservation requires an atomic provider permit"
                    )
            self.store._metric(db, "agent.model", {"outcome": "reserved"})
            if outcome is not None:
                raise StateConflict("model reservation cannot contain a response")
            if old.model_call_id != candidate.model_call_id:
                if call is not None:
                    raise StateConflict("logical model call id cannot be reused")
                call = AgentModelCallRow(
                    id=str(uuid4()),
                    session_id=row.id,
                    model_call_id=candidate.model_call_id,
                    round_index=candidate.budget.model_rounds,
                    status="inflight",
                    attempts=1,
                    usage=None,
                    created_at=now,
                    updated_at=now,
                )
                db.add(call)
            else:
                if call is None or call.status != "failed":
                    raise StateConflict("only confirmed failed model calls can retry")
                call.status, call.attempts, call.usage, call.updated_at = (
                    "inflight",
                    call.attempts + 1,
                    None,
                    now,
                )
            db.flush()  # ModelCall precedes Attempt (composite foreign key).
            db.add(
                AgentModelAttemptRow(
                    session_id=row.id,
                    model_call_id=call.model_call_id,
                    attempt_index=call.attempts,
                    outcome="inflight",
                    accounting="recorded",
                    usage=ModelUsage().model_dump(mode="json"),
                    created_at=now,
                    context_projection=transition.context_projection.manifest.model_dump(
                        mode="json"
                    )
                    if transition.context_projection is not None
                    else None,
                    request_contract=transition.request_contract.model_dump(mode="json")
                    if transition.request_contract is not None
                    else None,
                )
            )
            if grant is not None:
                if grant.permit.attempt_index != call.attempts:
                    raise StateConflict("provider permit attempt index differs")
                db.flush()
                self.admission.record(db, grant)
        if outcome is not None:
            self.store._metric(db, "agent.model", {"outcome": outcome.status})
            if (
                call is None
                or outcome.model_call_id != old.model_call_id
                or call.status not in {"inflight", "unknown"}
            ):
                raise StateConflict("model outcome does not match a pending call")
            attempt = db.scalar(
                select(AgentModelAttemptRow)
                .where(
                    AgentModelAttemptRow.session_id == row.id,
                    AgentModelAttemptRow.model_call_id == call.model_call_id,
                    AgentModelAttemptRow.attempt_index == call.attempts,
                )
                .with_for_update()
            )
            if attempt is None:
                raise StateConflict("model attempt reservation is missing")
            raw = checked_json(outcome.usage, MAX_USAGE_BYTES)
            try:
                usage = self.usage_mappings.normalize(raw, outcome.normalized_usage)
            except ValueError:
                raise AgentDataError("provider usage mapping is invalid") from None
            step = db.get(StepRow, row.step_row_id)
            binding = UsageGovernanceBinding.model_validate(
                db.get(RunRow, step.run_id).usage_governance or {}
            )
            if (
                usage.mapping is not None
                and binding.usage_mappings is not None
                and usage.mapping not in binding.usage_mappings
            ):
                raise AgentDataError("provider usage mapping differs from Run binding")
            checked_json(usage.model_dump(mode="json"), MAX_USAGE_BYTES)
            if raw is None and usage.source != "unavailable":
                raise AgentDataError("normalized usage requires raw measurements")
            if outcome.status == "unknown" and (
                raw is not None or usage != ModelUsage()
            ):
                raise StateConflict("unknown outcome cannot claim measured usage")
            if attempt.outcome == "unknown":
                if (
                    outcome.status != "unknown"
                    or raw is not None
                    or (outcome.model is not None or outcome.request_id is not None)
                ):
                    raise StateConflict(
                        "unknown attempt cannot be automatically confirmed"
                    )
            elif attempt.outcome == "inflight":
                attempt.outcome, attempt.raw_usage, attempt.usage = (
                    outcome.status,
                    raw,
                    usage.model_dump(mode="json"),
                )
                attempt.model, attempt.request_id = outcome.model, outcome.request_id
                attempt.confirmed_at = now
            else:
                raise StateConflict("model attempt already confirmed")
            if outcome.status == "succeeded" and candidate.status not in {
                AgentStatus.ACTIONS_PENDING,
                AgentStatus.SUCCEEDED,
            }:
                raise StateConflict(
                    "model response requires actions or agent completion"
                )
            if outcome.status == "failed" and candidate.status not in {
                AgentStatus.READY_MODEL,
                AgentStatus.FAILED,
                AgentStatus.EXHAUSTED,
            }:
                raise StateConflict(
                    "failed model outcome requires retry-ready or terminal state"
                )
            if outcome.status == "unknown" and candidate.status not in {
                AgentStatus.MODEL_INFLIGHT,
                AgentStatus.FAILED,
                AgentStatus.EXHAUSTED,
                AgentStatus.CANCELLED,
            }:
                raise StateConflict("unknown model outcome cannot authorize a retry")
            call.status, call.usage, call.updated_at = (
                outcome.status,
                raw,
                now,
            )
            self.admission.confirm(db, row, call, outcome, task)
        elif (
            old.status == AgentStatus.MODEL_INFLIGHT
            and candidate.status != AgentStatus.MODEL_INFLIGHT
        ):
            raise StateConflict("leaving model execution requires a model outcome")
        replies = [m for m in transition.messages if m.role == "assistant"]
        if len(replies) != int(outcome is not None and outcome.status == "succeeded"):
            raise StateConflict("model success requires exactly one assistant message")
        if replies and (
            replies[0].model_call_id != candidate.model_call_id
            or replies[0].payload.get("action_ids", [])
            != [a.id for a in candidate.actions]
        ):
            raise StateConflict("assistant response does not match the action batch")

    def _commit(self, task, transition, mode, output=None, retry_delay_seconds=None):
        status = transition.snapshot.status
        if status == AgentStatus.WAITING_APPROVAL:
            raise StateConflict("approval waiting requires a durable approval request")
        allowed = {
            "transition": {
                AgentStatus.READY_MODEL,
                AgentStatus.MODEL_INFLIGHT,
                AgentStatus.ACTIONS_PENDING,
            },
            "complete": {AgentStatus.SUCCEEDED},
            "fail": {AgentStatus.FAILED, AgentStatus.EXHAUSTED},
            "suspend": {
                AgentStatus.WAITING_APPROVAL,
                AgentStatus.WAITING_RECONCILIATION,
            },
            "defer": {AgentStatus.READY_MODEL},
        }
        with self._owned_transaction(task) as (db, run, step):
            if status not in allowed[mode]:
                raise StateConflict(
                    "agent state requires its matching workflow operation"
                )
            encoded = json_bytes(output) if mode == "complete" else None
            row = self._session(db, step)
            if row is None:
                raise StateConflict("agent session is not initialized")
            if mode == "defer":
                old_state = self._read(db, row)
                old = old_state.snapshot
                if (
                    old.status != AgentStatus.MODEL_INFLIGHT
                    or not old_state.model_calls
                    or old_state.model_calls[-1].status != "inflight"
                    or transition.model_result is None
                    or transition.model_result.status != "failed"
                    or transition.error_code != "ModelRetryable"
                    or transition.messages
                    or transition.arguments
                    or transition.observations
                    or transition.authorizations
                    or transition.snapshot.budget.model_attempts
                    >= transition.snapshot.budget.max_model_attempts
                ):
                    raise StateConflict(
                        "deferred retry requires confirmed model failure and remaining attempts"
                    )
            result = self._apply(db, run, step, row, transition, task=task)
            if mode == "complete":
                if result.observed_usage is not None:
                    output = {
                        **(output if isinstance(output, dict) else {"value": output}),
                        "observed_usage": result.observed_usage.model_dump(mode="json"),
                    }
                    encoded = json_bytes(output)
                spec = next(
                    s
                    for s in WorkflowSpec.model_validate(run.spec).steps
                    if s.id == step.key
                )
                self.store._complete_step(db, run, step, output, encoded, spec.artifact)
            elif mode == "defer":
                row.continuation_pending = True
                step.status, step.error = "ready", "ModelRetryable"
                step.available_at = self.store._now(db) + retry_delay_seconds
                if (
                    transition.model_result.backpressure is not None
                    and row.provider_key
                ):
                    policy_row = db.get(ProviderAdmissionRow, row.provider_key)
                    step.available_at = max(
                        step.available_at, policy_row.cooldown_until
                    )
                step.token, step.lease_until = None, None
                self.store._event(
                    db,
                    run.id,
                    "agent.retry_scheduled",
                    {
                        "step": step.key,
                        "model_call_id": result.snapshot.model_call_id,
                        "revision": result.snapshot.revision,
                        "available_at": step.available_at,
                        "error_code": "ModelRetryable",
                    },
                )
            elif mode in {"fail", "suspend"}:
                step.status = "failed" if mode == "fail" else "waiting"
                step.error = transition.error_code or (
                    "AgentExhausted"
                    if status == AgentStatus.EXHAUSTED
                    else ("AgentFailed" if mode == "fail" else None)
                )
                step.token, step.lease_until = None, None
                self.store._event(
                    db,
                    run.id,
                    f"step.{step.status}",
                    {"step": step.key, "agent_state": status.value},
                )
                db.flush()
                self.store._schedule(db, run)
        return result

    def commit_transition(
        self, task: Task, transition: AgentTransition
    ) -> AgentSession:
        return self._commit(task, transition, "transition")

    def complete_agent_step(
        self, task: Task, transition: AgentTransition, output: JsonValue
    ) -> AgentSession:
        return self._commit(task, transition, "complete", output)

    def fail_agent_step(self, task: Task, transition: AgentTransition) -> AgentSession:
        return self._commit(task, transition, "fail")

    def suspend_agent_step(
        self, task: Task, transition: AgentTransition
    ) -> AgentSession:
        return self._commit(task, transition, "suspend")

    def defer_model_retry(
        self, task: Task, transition: AgentTransition, retry_delay_seconds: float
    ) -> AgentSession:
        if (
            type(retry_delay_seconds) not in {int, float}
            or not math.isfinite(retry_delay_seconds)
            or not 0 < retry_delay_seconds <= 3600
        ):
            raise AgentDataError(
                "retry delay must be finite and within (0, 3600] seconds"
            )
        return self._commit(
            task, transition, "defer", retry_delay_seconds=retry_delay_seconds
        )

    def _interrupt(self, db, run, step, row, status, error):
        state = self._read(db, row)
        old = state.snapshot
        if old.status.is_terminal:
            raise StateConflict("cannot interrupt a terminal agent")
        actions, observations = [], []
        for action in old.actions:
            if action.status == ActionStatus.INFLIGHT:
                action = action.transition(ActionStatus.UNKNOWN, error_code=error)
                observations.append(
                    ActionObservation(
                        sequence=len(state.messages) + len(observations),
                        action_id=action.id,
                        result=None,
                    )
                )
            actions.append(action)
        outcome = None
        if (
            old.status == AgentStatus.MODEL_INFLIGHT
            and state.model_calls[-1].status == "inflight"
        ):
            outcome = ModelCallResult(model_call_id=old.model_call_id, status="unknown")
        # 已记录 UNKNOWN 的模型在终止时仍需显式携带其未知结果。
        if (
            old.status == AgentStatus.MODEL_INFLIGHT
            and status != AgentStatus.MODEL_INFLIGHT
        ):
            outcome = ModelCallResult(model_call_id=old.model_call_id, status="unknown")
        candidate = AgentSnapshot.model_validate(
            {
                **old.model_dump(),
                "revision": old.revision + 1,
                "status": status,
                "actions": tuple(actions),
            }
        )
        return self._apply(
            db,
            run,
            step,
            row,
            AgentTransition(
                snapshot=candidate,
                observations=tuple(observations),
                model_result=outcome,
            ),
        )

    def _step_failure(self, db, run, step, spec, error):
        """仅由持有 Run/Step 锁的 Workflow Store 调用。"""
        row = self._session(db, step)
        if row is None:
            return False
        if re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.-]{0,79}", error) is None:
            error = "AgentExecutionError"
        state = self._read(db, row)
        if error == "LeaseExpired":
            self.store._metric(db, "runtime.failure", {"kind": "lease_expired"})
            if any(a.status == ActionStatus.INFLIGHT for a in state.snapshot.actions):
                row.continuation_pending = False
                self._interrupt(
                    db, run, step, row, AgentStatus.WAITING_RECONCILIATION, error
                )
                step.status, step.token, step.lease_until, step.error = (
                    "waiting",
                    None,
                    None,
                    error,
                )
                self.store._event(
                    db,
                    run.id,
                    "step.waiting",
                    {"step": step.key, "agent_state": "waiting_reconciliation"},
                )
                return True
        if error == "LeaseExpired" and step.attempts < spec.max_attempts:
            if state.snapshot.status == AgentStatus.MODEL_INFLIGHT:
                self._interrupt(db, run, step, row, AgentStatus.MODEL_INFLIGHT, error)
            return False  # 沿用 Step 的尝试上限/退避，下一 Worker 恢复同一 Agent。
        self._interrupt(db, run, step, row, AgentStatus.FAILED, error)
        step.status, step.token, step.lease_until, step.error = (
            "failed",
            None,
            None,
            error,
        )
        self.store._event(
            db,
            run.id,
            "step.failed",
            {"step": step.key, "error": error, "attempt": step.attempts},
        )
        return True

    def _cancel_step(self, db, run, step):
        row = self._session(db, step)
        if row is not None:
            row.continuation_pending = False
            self._interrupt(
                db, run, step, row, AgentStatus.CANCELLED, "WorkflowCancelled"
            )
            for approval in db.scalars(
                select(AgentActionApprovalRow).where(
                    AgentActionApprovalRow.session_id == row.id,
                    AgentActionApprovalRow.decision == "pending",
                )
            ):
                approval.decision = "cancelled"
                approval.decided_at = self.store._now(db)

    @contextmanager
    def _operator_transaction(self, session_id):
        try:
            with self.store.transaction() as db:
                identity = db.execute(
                    select(StepRow.run_id, StepRow.id)
                    .join(AgentSessionRow, AgentSessionRow.step_row_id == StepRow.id)
                    .where(AgentSessionRow.id == session_id)
                ).first()
                if identity is None:
                    raise NotFoundError("agent session not found")
                run = self.store._locked_run(db, identity[0])
                step = db.scalar(
                    select(StepRow).where(StepRow.id == identity[1]).with_for_update()
                )
                row = self._session(db, step)
                yield db, run, step, row, self._read(db, row)
        except IntegrityError:
            raise StateConflict("action review record conflicts") from None
        except SQLAlchemyError:
            raise AgentStoreUnavailable(
                "action review persistence unavailable"
            ) from None

    def _ready_review(self, db, run, step, row):
        # Normal human review continues this attempt; a crashed lease consumed it.
        row.continuation_pending = step.error != "LeaseExpired"
        step.status, step.token, step.lease_until = "ready", None, None
        step.available_at, step.error = self.store._now(db), None
        db.flush()
        self.store._schedule(db, run)

    def request_action_approval(
        self, task: Task, action_id: str, expected_revision: int
    ) -> AgentSession:
        with self._owned_transaction(task) as (db, run, step):
            row = self._session(db, step)
            if row is None:
                raise StateConflict("agent session is not initialized")
            state = self._read(db, row)
            stored = next((a for a in state.actions if a.action.id == action_id), None)
            if (
                state.snapshot.revision != expected_revision
                or state.snapshot.status != AgentStatus.ACTIONS_PENDING
                or stored is None
                or stored.action not in state.snapshot.actions
                or stored.action.status != ActionStatus.PROPOSED
                or stored.tool_binding is None
                or not stored.tool_binding.protected
                or any(
                    a.status == ActionStatus.INFLIGHT for a in state.snapshot.actions
                )
                or any(
                    a.decision == "pending" or a.action_id == action_id
                    for a in state.approvals
                )
            ):
                raise StateConflict("action cannot request approval in this state")
            binding, now = stored.tool_binding, self.store._now(db)
            db.add(
                AgentActionApprovalRow(
                    id=str(uuid4()),
                    session_id=row.id,
                    action_id=action_id,
                    tool_name=binding.tool_name,
                    tool_version=binding.tool_version,
                    tool_policy_digest=binding.tool_policy_digest,
                    arguments_digest=stored.action.arguments_digest,
                    decision="pending",
                    created_at=now,
                )
            )
            db.flush()
            candidate = AgentSnapshot.model_validate(
                {
                    **state.snapshot.model_dump(),
                    "revision": expected_revision + 1,
                    "status": AgentStatus.WAITING_APPROVAL,
                }
            )
            result = self._apply(
                db, run, step, row, AgentTransition(snapshot=candidate)
            )
            step.status, step.token, step.lease_until = "waiting", None, None
            self.store._event(
                db,
                run.id,
                "agent.approval_requested",
                {"step": step.key, "action_id": action_id},
            )
            db.flush()
            self.store._schedule(db, run)
        return result

    def get_action_approval(self, approval_id: str) -> ActionApproval:
        try:
            with self.store.transaction() as db:
                record = db.get(AgentActionApprovalRow, approval_id)
                if record is None:
                    raise NotFoundError("action approval not found")
                session_id = record.session_id
            with self._operator_transaction(session_id) as (
                _db,
                _run,
                _step,
                _row,
                state,
            ):
                return next(a for a in state.approvals if a.approval_id == approval_id)
        except SQLAlchemyError:
            raise AgentStoreUnavailable(
                "action review persistence unavailable"
            ) from None

    def decide_action(
        self, approval_id: str, decision: ApprovalDecision
    ) -> ActionApproval:
        decision = ApprovalDecision.model_validate(decision.model_dump())
        info = self.get_action_approval(approval_id)
        with self._operator_transaction(info.session_id) as (db, run, step, row, state):
            approval = next(a for a in state.approvals if a.approval_id == approval_id)
            if run.status == "cancelled":
                raise StateConflict("cancelled workflow cannot approve actions")
            if approval.decision != "pending":
                if (approval.decision, approval.actor, approval.reason) == (
                    decision.decision,
                    decision.actor,
                    decision.reason,
                ):
                    return approval
                raise StateConflict("action approval already has a different decision")
            if (
                run.status in {"failed", "succeeded"}
                or step.status != "waiting"
                or step.token is not None
                or state.snapshot.status != AgentStatus.WAITING_APPROVAL
                or state.snapshot.revision != decision.expected_revision
            ):
                raise StateConflict("action approval revision or waiting state changed")
            action = next(
                (a for a in state.snapshot.actions if a.id == approval.action_id), None
            )
            if action is None or action.status != ActionStatus.PROPOSED:
                raise StateConflict("action approval no longer matches a proposal")
            saved = db.get(AgentActionApprovalRow, approval_id)
            saved.decision, saved.actor, saved.reason = (
                decision.decision,
                decision.actor,
                decision.reason,
            )
            saved.decided_at = self.store._now(db)
            observations = ()
            actions = state.snapshot.actions
            if decision.decision == "rejected":
                actions = tuple(
                    a.transition(ActionStatus.DENIED, error_code="ActionRejected")
                    if a.id == action.id
                    else a
                    for a in actions
                )
                observations = (
                    ActionObservation(
                        sequence=len(state.messages), action_id=action.id, result=None
                    ),
                )
            candidate = AgentSnapshot.model_validate(
                {
                    **state.snapshot.model_dump(),
                    "revision": state.snapshot.revision + 1,
                    "status": AgentStatus.ACTIONS_PENDING,
                    "actions": actions,
                }
            )
            result = self._apply(
                db,
                run,
                step,
                row,
                AgentTransition(snapshot=candidate, observations=observations),
                operator=True,
            )
            self._ready_review(db, run, step, row)
            self.store._event(
                db,
                run.id,
                "agent.approval_decided",
                {
                    "step": step.key,
                    "action_id": action.id,
                    "decision": decision.decision,
                },
            )
            answer = next(a for a in result.approvals if a.approval_id == approval_id)
        return answer

    def reconcile_action(
        self, session_id: str, action_id: str, decision: ReconciliationDecision
    ) -> ActionReconciliation:
        decision = ReconciliationDecision.model_validate(
            checked_json(decision.model_dump(mode="json"))
        )
        with self._operator_transaction(session_id) as (db, run, step, row, state):
            duplicate = db.get(AgentActionReconciliationRow, decision.request_id)
            if duplicate is not None:
                record = self._reconciliation(duplicate)
                if (
                    record.session_id == session_id
                    and record.action_id == action_id
                    and ReconciliationDecision.model_validate(decision.model_dump())
                    == ReconciliationDecision.model_validate(
                        {
                            k: v
                            for k, v in record.model_dump().items()
                            if k in ReconciliationDecision.model_fields
                        }
                    )
                ):
                    return record
                raise StateConflict("reconciliation request id has different content")
            stored = next((a for a in state.actions if a.action.id == action_id), None)
            if stored is None:
                raise NotFoundError("agent action not found")
            old = state.snapshot
            if decision.expected_revision != old.revision:
                raise StateConflict("agent revision changed")
            terminal = old.status.is_terminal
            if stored.action.status != ActionStatus.UNKNOWN:
                raise StateConflict("reconciliation requires an unknown action")
            if not terminal and (
                run.status in {"failed", "succeeded", "cancelled"}
                or step.status != "waiting"
                or step.token is not None
                or old.status != AgentStatus.WAITING_RECONCILIATION
            ):
                raise StateConflict("action is not waiting for reconciliation")
            record = AgentActionReconciliationRow(
                id=decision.request_id,
                session_id=session_id,
                action_id=action_id,
                request_revision=old.revision,
                previous_status=stored.action.status.value,
                confirmed_status=decision.confirmed_status,
                result=checked_json(decision.result, MAX_RESULT_BYTES),
                error_code=decision.error_code,
                evidence_reference=decision.evidence_reference,
                actor=decision.actor,
                reason=decision.reason,
                applied=not terminal,
                created_at=self.store._now(db),
            )
            db.add(record)
            db.flush()
            if not terminal:
                action = stored.action.reconcile(
                    ActionStatus(decision.confirmed_status),
                    error_code=decision.error_code,
                )
                actions = tuple(action if a.id == action_id else a for a in old.actions)
                waiting = any(a.status == ActionStatus.UNKNOWN for a in actions)
                spec = next(
                    s
                    for s in WorkflowSpec.model_validate(run.spec).steps
                    if s.id == step.key
                )
                retries_exhausted = (
                    not waiting
                    and step.error == "LeaseExpired"
                    and step.attempts >= spec.max_attempts
                )
                candidate = AgentSnapshot.model_validate(
                    {
                        **old.model_dump(),
                        "revision": old.revision + 1,
                        "actions": actions,
                        "status": AgentStatus.WAITING_RECONCILIATION
                        if waiting
                        else AgentStatus.FAILED
                        if retries_exhausted
                        else AgentStatus.ACTIONS_PENDING,
                    }
                )
                self._apply(
                    db,
                    run,
                    step,
                    row,
                    AgentTransition(
                        snapshot=candidate,
                        observations=(
                            ActionObservation(
                                sequence=len(state.messages),
                                action_id=action_id,
                                result=decision.result,
                            ),
                        ),
                    ),
                    operator=True,
                )
                if retries_exhausted:
                    step.status, step.token, step.lease_until = "failed", None, None
                    self.store._event(
                        db,
                        run.id,
                        "step.failed",
                        {
                            "step": step.key,
                            "error": "LeaseExpired",
                            "attempt": step.attempts,
                        },
                    )
                    db.flush()
                    self.store._schedule(db, run)
                elif not waiting:
                    self._ready_review(db, run, step, row)
            else:
                self._read(db, row)  # 终态只追加审计，并校验累计体积。
            self.store._event(
                db,
                run.id,
                "agent.reconciled",
                {"step": step.key, "action_id": action_id, "applied": not terminal},
            )
            self.store._metric(
                db,
                "agent.reconciliation_outcome",
                {"status": decision.confirmed_status, "applied": not terminal},
            )
            answer = self._reconciliation(record)
        return answer

    def review_history(self, session_id: str) -> dict:
        with self._operator_transaction(session_id) as (_db, _run, _step, _row, state):
            return {
                "session_id": session_id,
                "revision": state.snapshot.revision,
                "approvals": state.approvals,
                "reconciliations": state.reconciliations,
            }

    def list_pending_action_approvals(
        self, run_id: str | None = None, limit: int = 100
    ) -> tuple[ActionApproval, ...]:
        try:
            with self.store.transaction() as db:
                query = (
                    select(AgentActionApprovalRow.id)
                    .join(
                        AgentSessionRow,
                        AgentSessionRow.id == AgentActionApprovalRow.session_id,
                    )
                    .join(StepRow, StepRow.id == AgentSessionRow.step_row_id)
                    .join(RunRow, RunRow.id == StepRow.run_id)
                    .where(
                        AgentActionApprovalRow.decision == "pending",
                        RunRow.status.not_in(["failed", "succeeded", "cancelled"]),
                    )
                )
                if run_id is not None:
                    query = query.where(RunRow.id == run_id)
                ids = list(
                    db.scalars(
                        query.order_by(
                            AgentActionApprovalRow.created_at, AgentActionApprovalRow.id
                        ).limit(limit)
                    )
                )
            return tuple(
                a
                for i in ids
                if (a := self.get_action_approval(i)).decision == "pending"
            )
        except SQLAlchemyError:
            raise AgentStoreUnavailable(
                "action review persistence unavailable"
            ) from None

    def list_unknown_actions(
        self, run_id: str | None = None, limit: int = 100
    ) -> list[dict]:
        try:
            with self.store.transaction() as db:
                query = (
                    select(AgentActionRow, AgentSessionRow, StepRow.run_id)
                    .join(
                        AgentSessionRow, AgentSessionRow.id == AgentActionRow.session_id
                    )
                    .join(StepRow, StepRow.id == AgentSessionRow.step_row_id)
                    .where(AgentActionRow.status == "unknown")
                )
                if run_id is not None:
                    query = query.where(StepRow.run_id == run_id)
                return [
                    {
                        "session_id": s.id,
                        "action_id": a.id,
                        "run_id": r,
                        "revision": s.revision,
                        "agent_status": AgentSnapshot.model_validate(
                            checked_json(s.snapshot)
                        ).status.value,
                        "tool_name": a.tool_name,
                        "arguments_digest": a.arguments_digest,
                        "idempotency_key": a.idempotency_key,
                    }
                    for a, s, r in db.execute(
                        query.order_by(
                            AgentActionRow.created_at, AgentActionRow.id
                        ).limit(limit)
                    )
                ]
        except ValidationError:
            raise StateConflict("stored agent data is inconsistent") from None
        except SQLAlchemyError:
            raise AgentStoreUnavailable(
                "action review persistence unavailable"
            ) from None
