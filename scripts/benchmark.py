"""可重复的小型基准；报告本机耗时，不把结果当作生产容量承诺。"""

import tempfile
import time
from pathlib import Path

from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec
from agentflow.infrastructure.persistence.store import Store


def main():
    with tempfile.TemporaryDirectory(prefix="agentflow-bench-") as directory:
        store = Store(f"sqlite:///{Path(directory).as_posix()}/bench.db")
        try:
            store.create_schema()
            workflow = store.register(
                WorkflowSpec(
                    name="benchmark",
                    steps=tuple(
                        StepSpec(
                            id=f"s{i}",
                            handler="echo",
                            depends_on=(f"s{i - 1}",) if i else (),
                        )
                        for i in range(100)
                    ),
                )
            )
            start = time.perf_counter()
            run_id = store.submit(workflow, {})
            while task := store.claim():
                store.complete(task, {"ok": True})
            duration = time.perf_counter() - start
            assert store.get_run(run_id)["status"] == "succeeded"
            print(
                f"SQLite / 100-step chain: {duration:.3f}s, {100 / duration:.1f} steps/s"
            )
        finally:
            store.close()


if __name__ == "__main__":
    main()
