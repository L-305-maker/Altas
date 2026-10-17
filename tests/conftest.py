import os

import pytest
from runtime_assertions import runtime_invariants as runtime_invariants


def pytest_addoption(parser):
    parser.addoption(
        "--run-live-provider",
        action="store_true",
        default=False,
        help="Enable paid live-provider acceptance; separate authorization and key required.",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "live_provider: explicitly opted-in paid provider acceptance"
    )


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-live-provider"):
        reason = "paid provider tests require explicit --run-live-provider"
    elif os.environ.get(
        "AGENTFLOW_LIVE_DEEPSEEK_AUTHORIZATION"
    ) != "one-paid-request" or not os.environ.get("AGENTFLOW_DEEPSEEK_API_KEY"):
        reason = (
            "LIVE PROVIDER NOT VERIFIED: explicit paid authorization and key required"
        )
    else:
        return
    for item in items:
        if item.get_closest_marker("live_provider"):
            item.add_marker(pytest.mark.skip(reason=reason))
