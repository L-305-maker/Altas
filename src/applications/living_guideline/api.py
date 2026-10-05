"""Authenticated Living Guideline business routes."""

from fastapi import APIRouter, Header, Request

from applications.living_guideline.schemas import GuidelineInput

router = APIRouter()


@router.post("/api/guidelines", status_code=201)
def guideline(
    body: GuidelineInput,
    request: Request,
    idempotency_key: str | None = Header(default=None, max_length=128),
):
    return {
        "id": request.app.state.store.submit(
            request.app.state.guideline_id, body.model_dump(), idempotency_key
        )
    }
