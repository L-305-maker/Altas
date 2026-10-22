"""One explicitly authorized paid DeepSeek request, using only a fixed public prompt.

No hidden retries: max_model_attempts=1. Existing provider caps output at 4096
tokens, request timeout 45s. Input is fixed and below 1KB. A monetary cap must be
enforced on the dedicated provider account; this test cannot infer changing prices
or prevent billing for an UNKNOWN request. No response text/IDs/usage values print.
"""

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path

from sqlalchemy import select

from agentflow.application.agents.durable_runner import DurableAgentRunner
from agentflow.application.orchestration.worker import HandlerRegistry, Worker
from agentflow.application.ports.model_request import PreparedModelRequest
from agentflow.application.tools.runtime import ToolRuntime
from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec
from agentflow.infrastructure.models.deepseek import (
    DEFAULT_MODEL,
    SUPPORTED_MODELS,
    DeepSeekProvider,
)
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.models import (
    AgentModelAttemptRow,
    AgentSessionRow,
)
from agentflow.infrastructure.persistence.store import Store


def authorized():
    return os.environ.get(
        "AGENTFLOW_LIVE_DEEPSEEK_AUTHORIZATION"
    ) == "one-paid-request" and bool(os.environ.get("AGENTFLOW_DEEPSEEK_API_KEY"))


def versioned_report(report, version):
    if version == 1:
        return {
            k: v
            for k, v in report.items()
            if k not in {"usage_record_verified", "attempt_outcome"}
        }
    smoke = (
        "PASS"
        if report.get("valid_assistant") and report.get("provider_request_accepted")
        else "FAIL"
    )
    matched = report.get("model_identity_matches")
    identity = (
        "PASS" if matched is True else ("FAIL" if matched is False else "NOT_VERIFIED")
    )
    result = {
        **report,
        "schema_version": 2,
        "request_smoke_status": smoke,
        "provider_identity_acceptance": identity,
        "model_identity_status": identity,
        "durable_attempt_status": "PASS"
        if report.get("durable_attempt_confirmed")
        else "NOT_VERIFIED",
        "artifact_status": "PASS"
        if report.get("durable_step_and_artifact")
        else "NOT_VERIFIED",
        "usage_record_status": "PASS"
        if report.get("usage_record_verified")
        else "NOT_VERIFIED",
    }
    checks = (
        smoke,
        identity,
        result["durable_attempt_status"],
        result["artifact_status"],
        result["usage_record_status"],
    )
    result["status"] = (
        "FAIL"
        if report.get("status") == "FAIL" or "FAIL" in checks
        else ("PASS" if all(check == "PASS" for check in checks) else "NOT_VERIFIED")
    )
    return result


async def verify(provider, url, *, progress=None, report_version=1):
    if report_version not in {1, 2}:
        raise ValueError("UnsupportedLiveReportVersion")
    metadata, calls = progress if progress is not None else {}, 0
    metadata.update(
        requests=0,
        provider_request_accepted=None,
        valid_assistant=None,
        usage_present=None,
        model_identity_matches=None,
        durable_attempt_confirmed=None,
        durable_step_and_artifact=None,
    )

    class Once:
        def prepare_request(self, messages, tools, projection):
            if calls or tools:
                raise ValueError("LiveRequestLimit")
            prepared = provider.prepare_request(messages, tools, projection)

            async def execute():
                nonlocal calls
                if calls:
                    raise ValueError("LiveRequestLimit")
                calls += 1
                metadata["requests"] = calls
                response = await prepared.execute()
                metadata.update(
                    provider_request_accepted=True,
                    valid_assistant=bool(
                        response.assistant_message.role == "assistant"
                        and response.finish_reason == "stop"
                        and response.assistant_message.content
                        and not response.assistant_message.tool_calls
                    ),
                    usage_present=response.usage is not None,
                    request_id_present=response.request_id is not None,
                    model_present=response.model is not None,
                    model_identity_matches=(
                        response.model == prepared.request.contract.model
                    )
                    if response.model is not None
                    else None,
                )
                assert metadata["valid_assistant"]
                return response

            return PreparedModelRequest(prepared.request, execute)

    store = Store(url)
    try:
        store.create_schema()
        run = store.submit(
            store.register(
                WorkflowSpec(
                    name="live-public-smoke",
                    steps=(
                        StepSpec(
                            id="agent",
                            handler="live",
                            artifact=True,
                            max_attempts=1,
                            timeout=60,
                            parameters={
                                "agent": {
                                    "max_model_attempts": 1,
                                    "max_model_rounds": 1,
                                    "max_tool_calls": 0,
                                }
                            },
                        ),
                    ),
                )
            ),
            {},
        )
        handlers = HandlerRegistry()

        async def handler(task):
            return await DurableAgentRunner(
                SQLAgentStore(store), Once(), ToolRuntime()
            ).run(task, "Reply with READY. Do not request any tools.", allowed=set())

        handlers.register("live", handler)
        async with asyncio.timeout(65):
            assert await Worker(store, handlers, lease_seconds=15).tick()
        succeeded = store.get_run(run)["status"] == "succeeded"
        artifacts = len(store.artifacts(run))
        if report_version == 1:
            assert succeeded
            assert artifacts == 1
        with store.transaction() as db:
            snapshot = db.scalar(select(AgentSessionRow)).snapshot
            attempts = db.scalars(select(AgentModelAttemptRow)).all()
            assert len(attempts) == 1
            if report_version == 1:
                assert attempts[0].outcome == "succeeded"
            assert attempts[0].request_contract is not None
            if report_version == 1:
                assert (
                    attempts[0].model is None
                    or attempts[0].model == attempts[0].request_contract["model"]
                )
            metadata["usage_record_verified"] = (
                attempts[0].raw_usage is not None
            ) == bool(metadata["usage_present"]) and attempts[0].usage is not None
            metadata["attempt_outcome"] = attempts[0].outcome
        assert snapshot["budget"]["model_attempts"] == 1
        metadata.update(
            durable_attempt_confirmed=metadata["attempt_outcome"]
            in {"succeeded", "failed", "unknown"},
            durable_step_and_artifact=succeeded and artifacts == 1,
        )
        return versioned_report(
            {
                "status": "PASS" if succeeded else "FAIL",
                "requests": calls,
                "finish_reason": "stop" if metadata["valid_assistant"] else None,
                "durable_attempt_confirmed": True,
                "durable_step_and_artifact": True,
                "model_identity_status": "PASS"
                if metadata["model_identity_matches"]
                else "NOT_VERIFIED",
                **metadata,
            },
            report_version,
        )
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-paid-request", action="store_true")
    parser.add_argument("--model", choices=SUPPORTED_MODELS, default=DEFAULT_MODEL)
    parser.add_argument("--report-version", type=int, choices=(1, 2), default=1)
    args = parser.parse_args()
    if not args.allow_paid_request or not authorized():
        report = {"status": "LIVE PROVIDER NOT VERIFIED", "requests": 0}
        if args.report_version == 2:
            report.update(
                schema_version=2,
                request_smoke_status="NOT_VERIFIED",
                provider_identity_acceptance="NOT_VERIFIED",
                durable_attempt_status="NOT_VERIFIED",
                artifact_status="NOT_VERIFIED",
                usage_record_status="NOT_VERIFIED",
            )
        print(json.dumps(report))
        return 1 if args.allow_paid_request else 0
    progress = {}
    try:
        # Fixed HTTPS endpoint and allowlisted model; no credential redirection.
        provider = DeepSeekProvider(
            os.environ["AGENTFLOW_DEEPSEEK_API_KEY"], args.model
        )
        with tempfile.TemporaryDirectory(prefix="agentflow-live-") as directory:
            report = asyncio.run(
                verify(
                    provider,
                    f"sqlite:///{(Path(directory) / 'live.db').as_posix()}",
                    progress=progress,
                    report_version=args.report_version,
                )
            )
        print(json.dumps(report))
        return (
            int(report["status"] != "PASS")
            if args.report_version == 2
            else int(report["status"] == "FAIL")
        )
    except Exception:
        # No traceback, exception message, prompt, key, response, or provider ID.
        print(
            json.dumps(
                versioned_report(
                    {
                        "status": "FAIL",
                        "requests": 0,
                        **progress,
                        "error": "LiveProviderAcceptanceFailed",
                    },
                    args.report_version,
                )
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
