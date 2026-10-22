import httpx
import pytest
from test_rc_tools import script

from agentflow.infrastructure.models.deepseek import DEFAULT_MODEL, DeepSeekProvider


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model,identity",
    [(None, "NOT_VERIFIED"), ("different-model", "FAIL"), (DEFAULT_MODEL, "PASS")],
)
async def test_v2_smoke_and_identity_are_separate(
    monkeypatch, tmp_path, model, identity
):
    module = script(monkeypatch, "verify_live_provider")
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "ready"},
                        "finish_reason": "stop",
                    }
                ],
                **({"model": model} if model is not None else {}),
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 1,
                    "total_tokens": 4,
                },
            },
        )

    provider = DeepSeekProvider(
        "synthetic-not-a-paid-key",
        DEFAULT_MODEL,
        transport=httpx.MockTransport(transport),
    )
    report = await module.verify(
        provider, f"sqlite:///{(tmp_path / 'smoke.db').as_posix()}", report_version=2
    )
    assert report["schema_version"] == 2
    assert report["request_smoke_status"] == "PASS"
    assert report["provider_identity_acceptance"] == identity
    assert report["status"] == identity
    assert (
        report["durable_attempt_status"]
        == report["artifact_status"]
        == report["usage_record_status"]
        == "PASS"
    )
    assert report["requests"] == len(requests) == 1
    assert "synthetic-not-a-paid-key" not in str(report)


@pytest.mark.parametrize(
    "flag,authorization,key",
    [(False, True, True), (True, False, True), (True, True, False)],
)
def test_v2_no_paid_authorization_never_constructs_provider(
    monkeypatch, capsys, flag, authorization, key
):
    module = script(monkeypatch, "verify_live_provider")
    monkeypatch.setenv(
        "AGENTFLOW_LIVE_DEEPSEEK_AUTHORIZATION",
        "one-paid-request" if authorization else "",
    )
    monkeypatch.setenv("AGENTFLOW_DEEPSEEK_API_KEY", "synthetic-key" if key else "")
    monkeypatch.setattr(
        module, "DeepSeekProvider", lambda *a, **kw: pytest.fail("NetworkMustNotStart")
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "verify_live_provider.py",
            "--report-version",
            "2",
            *(["--allow-paid-request"] if flag else []),
        ],
    )
    assert module.main() == int(flag)
    report = capsys.readouterr().out
    assert '"requests": 0' in report and "synthetic-key" not in report


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["rate_limit", "unknown"])
async def test_v2_failure_reports_durable_unknown_without_retry(
    monkeypatch, tmp_path, response
):
    module = script(monkeypatch, "verify_live_provider")
    requests = []

    def transport(request):
        requests.append(request)
        if response == "unknown":
            raise httpx.ReadTimeout("PRIVATE_RESPONSE", request=request)
        return httpx.Response(429, json={"error": "PRIVATE_RESPONSE"})

    provider = DeepSeekProvider(
        "synthetic-key", DEFAULT_MODEL, transport=httpx.MockTransport(transport)
    )
    result = await module.verify(
        provider, f"sqlite:///{(tmp_path / 'failed.db').as_posix()}", report_version=2
    )
    assert result["status"] == result["request_smoke_status"] == "FAIL"
    assert result["provider_identity_acceptance"] == "NOT_VERIFIED"
    assert result["durable_attempt_status"] == "PASS"
    assert result["attempt_outcome"] == (
        "unknown" if response == "unknown" else "failed"
    )
    assert result["artifact_status"] == "NOT_VERIFIED"
    assert result["requests"] == len(requests) == 1
    assert "PRIVATE" not in str(result) and "synthetic-key" not in str(result)


def test_v2_smoke_and_matching_identity_do_not_hide_unconfirmed_commit(monkeypatch):
    module = script(monkeypatch, "verify_live_provider")
    report = module.versioned_report(
        {
            "status": "FAIL",
            "requests": 1,
            "provider_request_accepted": True,
            "valid_assistant": True,
            "model_identity_matches": True,
            "durable_attempt_confirmed": None,
            "durable_step_and_artifact": None,
        },
        2,
    )
    assert (
        report["request_smoke_status"]
        == report["provider_identity_acceptance"]
        == "PASS"
    )
    assert report["status"] == "FAIL"
    assert report["artifact_status"] == "NOT_VERIFIED"


def test_v2_cli_missing_identity_is_incomplete_not_a_success_exit(monkeypatch, capsys):
    module = script(monkeypatch, "verify_live_provider")
    from agentflow.infrastructure.models.mock import MockProvider

    monkeypatch.setenv("AGENTFLOW_LIVE_DEEPSEEK_AUTHORIZATION", "one-paid-request")
    monkeypatch.setenv("AGENTFLOW_DEEPSEEK_API_KEY", "synthetic-key")
    monkeypatch.setattr(module, "DeepSeekProvider", lambda *a, **kw: MockProvider())
    monkeypatch.setattr(
        "sys.argv",
        ["verify_live_provider.py", "--allow-paid-request", "--report-version", "2"],
    )
    assert module.main() == 1
    output = capsys.readouterr().out
    assert '"status": "NOT_VERIFIED"' in output
    assert "synthetic-key" not in output
