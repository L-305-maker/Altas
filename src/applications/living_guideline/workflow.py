from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec

GUIDELINE = WorkflowSpec(
    name="living_guideline",
    version=1,
    steps=(
        StepSpec(id="extract", handler="guideline.extract", timeout=120),
        StepSpec(id="draft", handler="guideline.draft", depends_on=("extract",)),
        StepSpec(
            id="publish",
            handler="guideline.publish",
            depends_on=("draft",),
            requires_approval=True,
            artifact=True,
        ),
    ),
)
