"""Keep the framework independently usable as applications are added."""

import ast
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parents[2] / "src" / "agentflow"


def imports(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield node.module or ""
            yield from (f"{node.module or ''}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            if (
                isinstance(func, ast.Name)
                and func.id == "__import__"
                or isinstance(func, ast.Attribute)
                and func.attr == "import_module"
            ) and isinstance(node.args[0], ast.Constant):
                yield str(node.args[0].value)


@pytest.mark.parametrize("package", ["applications", "examples"])
def test_agentflow_does_not_import_application_packages(package):
    for path in CORE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for module in imports(tree):
            assert package not in module.split("."), (path, module)


def test_agentflow_has_no_business_identifiers():
    forbidden = (
        "living_guideline",
        "GuidelineInput",
        "GuidelineHandlers",
        "GUIDELINE",
        "guideline.",
    )
    for path in CORE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Ignore genuine docstrings; executable strings and identifiers remain guarded.
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                if ast.get_docstring(node) is not None:
                    node.body = node.body[1:]
        source = ast.unparse(tree)
        assert not any(word in source for word in forbidden), path
