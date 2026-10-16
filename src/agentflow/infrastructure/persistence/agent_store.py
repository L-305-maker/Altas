"""复用 Workflow Store 的连接、锁和时钟；数据库不保证外部副作用 exactly-once。"""

import json
import re
from contextlib import contextmanager
from uuid import uuid4

from pydantic import JsonValue, ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from agentflow.application.ports.agent_store import (
    ActionObservation,
    AgentDataError,
    AgentMessage,
    AgentSession,
    AgentStoreUnavailable,
    AgentTransition,
    LostLease,
    ModelCallRecord,
    ModelCallResult,
    StateConflict,
    StoredAction,
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
    AgentModelCallRow,
    AgentSessionRow,
    RunRow,
    StepRow,
)
from agentflow.infrastructure.persistence.store import NotFoundError, Store, json_bytes

MAX_MESSAGE_BYTES = 100_000
MAX_ARGUMENT_BYTES = 100_000
MAX_RESULT_BYTES = 100_000
MAX_SESSION_BYTES = 8_000_000


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
    def __init__(self, store: Store) -> None:
        self.store = store

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

    def _read(self, db, row):
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
            self._check_capacity(initial, state)
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

    def _check_capacity(self, initial, state):
        data = {
            "initial": initial.model_dump(mode="json"),
            "state": state.model_dump(mode="json"),
        }
        checked_json(data, MAX_SESSION_BYTES)
        if state.snapshot.status.is_terminal:
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
        AgentSnapshot.model_validate(snapshot)
        checked_json(snapshot, 2_000_000 - 1024)
        data["control_margin"] = "x" * 1024
        checked_json(data, MAX_SESSION_BYTES)

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
            row = self._session(db, step)
            if row is None:
                now = self.store._now(db)
                row = AgentSessionRow(
                    id=initial_snapshot.id,
                    step_row_id=step.id,
                    initial_snapshot=data,
                    snapshot=data,
                    revision=0,
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
            result = self._read(db, row)
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

    def _apply(self, db, run, step, row, transition, *, operator=False):
        try:
            transition = AgentTransition.model_validate(
                checked_json(transition.model_dump(mode="json"))
            )
        except ValidationError:
            raise AgentDataError("invalid agent transition data") from None
        old_state = self._read(db, row)
        old, candidate = old_state.snapshot, transition.snapshot
        try:
            old.advance(candidate)
        except (ValueError, InvalidStateTransitionError):
            raise StateConflict("invalid agent revision or state transition") from None
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
        self._model_update(db, row, old, transition, now)
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
        return self._read(db, row)

    def _model_update(self, db, row, old, transition, now):
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
        if outcome is not None:
            if (
                call is None
                or outcome.model_call_id != old.model_call_id
                or call.status not in {"inflight", "unknown"}
            ):
                raise StateConflict("model outcome does not match a pending call")
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
                checked_json(outcome.usage, MAX_MESSAGE_BYTES),
                now,
            )
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

    def _commit(self, task, transition, mode, output=None):
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
            result = self._apply(db, run, step, row, transition)
            if mode == "complete":
                spec = next(
                    s
                    for s in WorkflowSpec.model_validate(run.spec).steps
                    if s.id == step.key
                )
                self.store._complete_step(db, run, step, output, encoded, spec.artifact)
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
        if error == "LeaseExpired" and step.attempts < spec.max_attempts:
            if any(a.status == ActionStatus.INFLIGHT for a in state.snapshot.actions):
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

    def _ready_review(self, db, run, step):
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
            self._ready_review(db, run, step)
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
            if not terminal and (
                run.status in {"failed", "succeeded", "cancelled"}
                or step.status != "waiting"
                or step.token is not None
                or old.status != AgentStatus.WAITING_RECONCILIATION
                or stored.action.status != ActionStatus.UNKNOWN
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
                candidate = AgentSnapshot.model_validate(
                    {
                        **old.model_dump(),
                        "revision": old.revision + 1,
                        "actions": actions,
                        "status": AgentStatus.WAITING_RECONCILIATION
                        if waiting
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
                if not waiting:
                    self._ready_review(db, run, step)
            else:
                self._read(db, row)  # 终态只追加审计，并校验累计体积。
            self.store._event(
                db,
                run.id,
                "agent.reconciled",
                {"step": step.key, "action_id": action_id, "applied": not terminal},
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
