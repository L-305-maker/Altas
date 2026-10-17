"""审计消息不可变；模型上下文按当前持久化结果重建。"""

import json

from pydantic import ValidationError

from agentflow.application.ports.agent_store import AgentSession, StateConflict
from agentflow.application.ports.model_provider import AssistantMessage, ModelToolCall


def context_bytes(messages: list[dict], tools: list[dict]) -> int:
    """完整消息与工具 Schema 的紧凑 JSON UTF-8 大小；不包含供应商配置。"""
    payload = {"messages": messages}
    if tools:
        payload["tools"] = tools
    return len(
        json.dumps(
            payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    )


def parse_tool_arguments(text: str) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        value = json.loads(text, object_pairs_hook=unique)
        if (
            not isinstance(value, dict)
            or len(json.dumps(value, allow_nan=False).encode()) > 100_000
        ):
            raise ValueError
        return value
    except (ValueError, TypeError, RecursionError):
        raise ValueError("invalid tool arguments") from None


def mapped_calls(session: AgentSession) -> dict[str, ModelToolCall]:
    """只接受持久化的有序映射，不能从供应商 ID 猜测核心 Action 身份。"""
    result = {}
    try:
        for entry in session.messages:
            if entry.role != "assistant":
                continue
            message = AssistantMessage.model_validate(entry.payload["message"])
            batch = [
                a
                for a in session.actions
                if a.action.model_call_id == entry.model_call_id
            ]
            if entry.payload["action_ids"] != [a.action.id for a in batch] or len(
                batch
            ) != len(message.tool_calls):
                raise ValueError
            for index, (stored, call) in enumerate(
                zip(batch, message.tool_calls, strict=True)
            ):
                if (
                    stored.call_index != index
                    or stored.action.tool_name != call.function.name
                    or stored.action.id in result
                ):
                    raise ValueError
                try:
                    arguments = parse_tool_arguments(call.function.arguments)
                except ValueError:
                    if stored.arguments or stored.action.status not in {
                        "proposed",
                        "denied",
                    }:
                        raise ValueError from None
                    if (
                        stored.action.status == "denied"
                        and stored.action.error_code != "ToolArgumentsInvalid"
                    ):
                        raise ValueError from None
                else:
                    if arguments != stored.arguments:
                        raise ValueError
                result[stored.action.id] = call
        if result.keys() != {a.action.id for a in session.actions}:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise StateConflict("agent tool call mapping is inconsistent") from None
    return result


class UnresolvedActions(Exception):
    pass


class AgentContextBuilder:
    def build(self, session: AgentSession) -> list[dict]:
        mapped_calls(session)
        if any(not a.action.status.is_terminal for a in session.actions):
            raise UnresolvedActions(
                "tool outcomes must be resolved before model execution"
            )
        actions = {a.action.id: a for a in session.actions}
        result = []
        seen = set()
        try:
            for entry in session.messages:
                if entry.role in {"system", "user"}:
                    content = entry.payload["content"]
                    if not isinstance(content, str):
                        raise ValueError
                    result.append({"role": entry.role, "content": content})
                elif entry.role == "assistant":
                    message = AssistantMessage.model_validate(entry.payload["message"])
                    ids = entry.payload["action_ids"]
                    if len(ids) != len(message.tool_calls):
                        raise ValueError
                    replay = message.model_dump(mode="json", exclude_none=True)
                    if not message.tool_calls:
                        replay.pop("tool_calls", None)
                    for index, (action_id, call) in enumerate(
                        zip(ids, message.tool_calls, strict=True)
                    ):
                        stored = actions[action_id]
                        if (
                            action_id in seen
                            or stored.call_index != index
                            or stored.action.model_call_id != entry.model_call_id
                            or stored.action.tool_name != call.function.name
                        ):
                            raise ValueError
                        seen.add(action_id)
                        # 原供应商 ID 保留在审计消息内。发送时用核心 ID 消除跨轮次碰撞。
                        replay["tool_calls"][index]["id"] = action_id
                    result.append(replay)
                    for action_id in ids:
                        stored = actions[action_id]
                        result.append(
                            {
                                "role": "tool",
                                "tool_call_id": action_id,
                                "content": json.dumps(
                                    {
                                        "status": stored.action.status.value,
                                        "error_code": stored.action.error_code,
                                        "result": stored.result,
                                    },
                                    ensure_ascii=False,
                                    allow_nan=False,
                                    separators=(",", ":"),
                                ),
                            }
                        )
                # tool 审计记录可能有 UNKNOWN 和最终结果；只从 StoredAction 回放一次。
            if seen != actions.keys():
                raise ValueError
        except (ValidationError, ValueError, KeyError, TypeError):
            raise StateConflict(
                "agent transcript or action mapping is inconsistent"
            ) from None
        return result
