"""离线回归评测，仅衡量可追溯性与流程完成率，不宣称模型/医学质量。"""

import asyncio
import json
import tempfile
from pathlib import Path

from agentflow.application.orchestration.worker import Worker
from agentflow.applications.living_guideline import GUIDELINE
from agentflow.bootstrap import build_handlers
from agentflow.config import Settings
from agentflow.infrastructure.persistence.store import Store


async def main():
    cases = json.loads(
        (Path(__file__).parent / "datasets/evidence.json").read_text(encoding="utf-8")
    )
    with tempfile.TemporaryDirectory(prefix="agentflow-eval-") as directory:
        store = Store(f"sqlite:///{Path(directory).as_posix()}/eval.db")
        try:
            store.create_schema()
            workflow = store.register(GUIDELINE)
            worker = Worker(store, build_handlers(Settings(provider="mock")))
            passed = 0
            for case in cases:
                run_id = store.submit(workflow, case)
                await worker.tick()
                await worker.tick()
                approval = store.approvals(run_id)[0]
                store.decide(approval["id"], True, "offline-evaluator")
                await worker.tick()
                artifact = store.artifacts(run_id)[0]["content"]
                valid = all(
                    any(citation["quote"] in doc["text"] for doc in case["documents"])
                    for citation in artifact["citations"]
                )
                passed += valid and store.get_run(run_id)["status"] == "succeeded"
            print(
                json.dumps(
                    {
                        "mode": "offline",
                        "cases": len(cases),
                        "passed": passed,
                        "traceability_rate": passed / len(cases),
                    },
                    ensure_ascii=False,
                )
            )
            if passed != len(cases):
                raise SystemExit(1)
        finally:
            store.close()


if __name__ == "__main__":
    asyncio.run(main())
