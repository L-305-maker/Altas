import pytest

from runtime_assertions import runtime_invariants as runtime_invariants


def pytest_addoption(parser):
    parser.addoption(
        "--run-live-provider",
        action="store_true",
        default=False,
        help="Explicitly enable paid live-provider tests (requires separate authorization).",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "live_provider: explicitly opted-in paid provider acceptance"
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-live-provider"):
        return
    disabled = pytest.mark.skip(
        reason="paid provider tests require explicit --run-live-provider"
    )
    for item in items:
        if item.get_closest_marker("live_provider"):
            item.add_marker(disabled)
