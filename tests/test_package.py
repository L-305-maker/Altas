from importlib.metadata import version

import agentflow


def test_package_can_import_and_version_matches_metadata():
    assert agentflow is not None
    assert agentflow.__version__ == version("agentflow")
