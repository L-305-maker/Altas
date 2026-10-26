"""Keep the detached fixture on the documented public side of Core."""

import ast
import runpy
from pathlib import Path

from agentflow.runtime import WorkflowSpec

FIXTURE = Path(__file__).resolve().parents[2] / "examples/independent_consumer"


def test_consumer_only_imports_public_core_and_no_business_or_orm():
    for path in FIXTURE.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            for name in names:
                assert name.split(".")[0] not in {
                    "applications",
                    "sqlalchemy",
                    "tests",
                    "scripts",
                }
                if name.startswith("agentflow."):
                    assert name == "agentflow.runtime"


def test_independent_workflow_explicitly_requires_durable_and_is_a_dag():
    spec = runpy.run_path(str(FIXTURE / "consumer.py"))["workflow"]()
    assert isinstance(spec, WorkflowSpec)
    assert spec.steps[1].parameters["agent"]["mode"] == "durable_v1"
    assert spec.steps[2].depends_on == ("agent",) and spec.steps[2].artifact
