```
agentflow/
│
├── README.md
├── LICENSE
├── pyproject.toml
├── uv.lock
├── Makefile
├── alembic.ini
├── docker-compose.yml
├── .env.example
├── .gitignore
├── .pre-commit-config.yaml
│
├── .github/
│   └── workflows/
│       ├── test.yml
│       ├── lint.yml
│       └── docker.yml
│
├── docs/
│   ├── architecture.md
│   ├── concepts.md
│   ├── execution-model.md
│   ├── state-machine.md
│   ├── durability.md
│   ├── tool-system.md
│   ├── mcp.md
│   ├── sandbox.md
│   ├── observability.md
│   └── adr/
│       ├── 0001-postgres-queue.md
│       ├── 0002-at-least-once.md
│       └── 0003-domain-boundaries.md
│
├── migrations/
│   ├── env.py
│   └── versions/
│
├── src/
│   └── agentflow/
│       │
│       ├── __init__.py
│       ├── config.py
│       ├── exceptions.py
│       ├── logging.py
│       │
│       ├── domain/
│       │   │
│       │   ├── common/
│       │   │   ├── ids.py
│       │   │   ├── result.py
│       │   │   └── errors.py
│       │   │
│       │   ├── workflow/
│       │   │   ├── models.py
│       │   │   ├── states.py
│       │   │   ├── transitions.py
│       │   │   ├── graph.py
│       │   │   └── policies.py
│       │   │
│       │   ├── execution/
│       │   │   ├── models.py
│       │   │   ├── retry.py
│       │   │   └── checkpoint.py
│       │   │
│       │   ├── events/
│       │   │   ├── base.py
│       │   │   └── events.py
│       │   │
│       │   ├── tools/
│       │   │   ├── models.py
│       │   │   └── errors.py
│       │   │
│       │   ├── approvals/
│       │   │   ├── models.py
│       │   │   └── policies.py
│       │   │
│       │   └── artifacts/
│       │       └── models.py
│       │
│       ├── application/
│       │   │
│       │   ├── ports/
│       │   │   ├── workflow_repository.py
│       │   │   ├── job_queue.py
│       │   │   ├── event_store.py
│       │   │   ├── checkpoint_store.py
│       │   │   ├── artifact_store.py
│       │   │   ├── model_provider.py
│       │   │   ├── sandbox.py
│       │   │   └── clock.py
│       │   │
│       │   ├── orchestration/
│       │   │   ├── scheduler.py
│       │   │   ├── executor.py
│       │   │   ├── dependency_resolver.py
│       │   │   ├── recovery.py
│       │   │   └── cancellation.py
│       │   │
│       │   ├── agents/
│       │   │   ├── models.py
│       │   │   ├── runner.py
│       │   │   ├── loop.py
│       │   │   └── context.py
│       │   │
│       │   ├── tools/
│       │   │   ├── registry.py
│       │   │   ├── runtime.py
│       │   │   └── policies.py
│       │   │
│       │   ├── approvals/
│       │   │   └── service.py
│       │   │
│       │   ├── commands/
│       │   │   ├── create_workflow.py
│       │   │   ├── start_run.py
│       │   │   ├── cancel_run.py
│       │   │   └── resolve_approval.py
│       │   │
│       │   └── queries/
│       │       ├── get_run.py
│       │       └── list_runs.py
│       │
│       ├── infrastructure/
│       │   │
│       │   ├── persistence/
│       │   │   └── postgres/
│       │   │       ├── database.py
│       │   │       ├── models.py
│       │   │       ├── mappers.py
│       │   │       ├── repositories.py
│       │   │       └── unit_of_work.py
│       │   │
│       │   ├── queue/
│       │   │   └── postgres/
│       │   │       ├── models.py
│       │   │       └── queue.py
│       │   │
│       │   ├── events/
│       │   │   └── postgres.py
│       │   │
│       │   ├── models/
│       │   │   ├── mock.py
│       │   │   └── openai.py
│       │   │
│       │   ├── tools/
│       │   │   └── builtin/
│       │   │       ├── http.py
│       │   │       ├── filesystem.py
│       │   │       ├── python.py
│       │   │       └── database.py
│       │   │
│       │   ├── mcp/
│       │   │   ├── client.py
│       │   │   ├── server.py
│       │   │   └── adapter.py
│       │   │
│       │   ├── sandbox/
│       │   │   └── docker.py
│       │   │
│       │   ├── artifacts/
│       │   │   ├── local.py
│       │   │   └── postgres.py
│       │   │
│       │   └── telemetry/
│       │       ├── tracing.py
│       │       ├── metrics.py
│       │       └── logging.py
│       │
│       └── interfaces/
│           │
│           ├── api/
│           │   ├── app.py
│           │   ├── dependencies.py
│           │   ├── schemas/
│           │   └── routes/
│           │       ├── workflows.py
│           │       ├── runs.py
│           │       ├── approvals.py
│           │       └── events.py
│           │
│           ├── worker/
│           │   ├── main.py
│           │   ├── worker.py
│           │   └── heartbeat.py
│           │
│           └── cli/
│               ├── main.py
│               ├── run.py
│               └── worker.py
│
├── web/
│   ├── package.json
│   ├── next.config.ts
│   ├── app/
│   │   ├── workflows/
│   │   ├── runs/
│   │   └── approvals/
│   ├── components/
│   │   ├── workflow/
│   │   ├── run/
│   │   └── trace/
│   └── lib/
│       ├── api.ts
│       └── events.ts
│
├── tests/
│   │
│   ├── unit/
│   │   ├── domain/
│   │   ├── orchestration/
│   │   ├── agents/
│   │   └── tools/
│   │
│   ├── integration/
│   │   ├── postgres/
│   │   ├── queue/
│   │   ├── worker/
│   │   ├── mcp/
│   │   └── sandbox/
│   │
│   ├── e2e/
│   │   ├── test_workflow.py
│   │   ├── test_recovery.py
│   │   ├── test_approval.py
│   │   └── test_cancellation.py
│   │
│   ├── chaos/
│   │   ├── test_worker_crash.py
│   │   ├── test_timeout.py
│   │   └── test_duplicate_delivery.py
│   │
│   └── fixtures/
│
├── evals/
│   ├── datasets/
│   ├── evaluators/
│   └── runners/
│
├── examples/
│   │
│   ├── hello_workflow/
│   │   └── workflow.py
│   │
│   ├── research_agent/
│   │   └── workflow.py
│   │
│   └── living_guideline/
│       ├── workflow.py
│       ├── agents/
│       │   ├── extractor.py
│       │   ├── lineage.py
│       │   ├── evidence.py
│       │   ├── etd.py
│       │   └── verifier.py
│       └── tools/
│           ├── guideline_search.py
│           └── evidence_search.py
│
├── scripts/
│   ├── dev.sh
│   ├── migrate.sh
│   ├── seed.py
│   └── chaos_test.sh
│
└── deploy/
    ├── docker/
    │   ├── api.Dockerfile
    │   ├── worker.Dockerfile
    │   └── web.Dockerfile
    └── prometheus/
        └── prometheus.yml
```