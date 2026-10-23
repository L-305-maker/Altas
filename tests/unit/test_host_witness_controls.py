"""No staging connections, private keys or paid requests in collector tests."""

import json
from types import SimpleNamespace

import pytest
from test_rc_causal import acceptance as acceptance
from test_rc_causal import case_result
from test_rc_causal import causal as causal
from test_rc_tools import script


def test_database_unavailable_is_not_a_losing_lease_receipt(causal):
    result = case_result(
        causal, "A", lambda d: d["ops"][1].update(error_code="DatabaseUnavailable")
    )
    assert result.status != "PASS"


@pytest.mark.parametrize("command", ("run", "probe", "status"))
def test_default_refuses_staging_before_connect(monkeypatch, capsys, command):
    module = script(monkeypatch, "rc_host_witness")
    import rc_staging

    monkeypatch.setattr(
        rc_staging, "connect", lambda *a, **k: pytest.fail("UnauthorizedConnection")
    )
    monkeypatch.setattr("sys.argv", ["rc_host_witness.py", command])
    assert module.main() == 2
    output = capsys.readouterr()
    assert json.loads(output.out)["error"] == "WitnessCollectionRejected"


@pytest.mark.parametrize(
    "option", ("--claim-accepted", "--lost-lease", "--private-key")
)
def test_cli_has_no_handwritten_event_or_signing_input(monkeypatch, option):
    module = script(monkeypatch, "rc_host_witness")
    monkeypatch.setattr("sys.argv", ["rc_host_witness.py", "run", option])
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2


def test_consumed_challenge_cannot_start_collection(monkeypatch, causal, tmp_path):
    module = script(monkeypatch, "rc_host_witness")
    from rc_acceptance import consume_challenge, issue_challenge

    _, challenge, _, _, _, _ = causal
    registry = tmp_path / "registry"
    issue_challenge(registry, challenge)
    module.check_registration(registry, challenge)
    consume_challenge(registry, challenge)
    with pytest.raises(ValueError, match="ChallengeReplay"):
        module.check_registration(registry, challenge)


def test_export_rejects_wrong_trust_pin_without_connection(
    monkeypatch, causal, tmp_path, capsys
):
    module = script(monkeypatch, "rc_host_witness")
    _, challenge, _, _, _, _ = causal
    challenge_file = tmp_path / "challenge.json"
    challenge_file.write_text(challenge.model_dump_json(), encoding="utf-8")
    trust = tmp_path / "trust.json"
    trust.write_text('{"PRIVATE_TEST_CREDENTIAL":"never-output"}', encoding="utf-8")
    config = SimpleNamespace(environment_id=challenge.environment_id)
    monkeypatch.setattr(module, "context", lambda a: (config, None, None, None))
    monkeypatch.setattr(module, "matches", lambda *a: None)
    monkeypatch.setattr(
        module, "checked_plan", lambda *a: challenge.staging_batch_digest
    )
    monkeypatch.setattr(module, "check_registration", lambda *a: None)
    import rc_staging

    monkeypatch.setattr(
        rc_staging, "connect", lambda *a, **k: pytest.fail("OfflineExportConnected")
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "rc_host_witness.py",
            "export",
            "--environment-id",
            str(challenge.environment_id),
            "--challenge",
            str(challenge_file),
            "--trust",
            str(trust),
            "--trust-sha256",
            "0" * 64,
        ],
    )
    assert module.main() == 2
    assert (
        capsys.readouterr().out.strip()
        == '{"status": "NOT_VERIFIED", "error": "WitnessCollectionRejected"}'
    )
