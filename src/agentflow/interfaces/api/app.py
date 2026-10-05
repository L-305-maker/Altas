"""认证 API。HTTP 只负责输入校验与资源编排，实际执行由独立 worker 完成。

单租户 operator token 模型：持有 token 的调用者被视为可信部署操作者。
不是多租户授权系统；浏览器不保存 token 到 localStorage，也不把 token 放 URL。
"""

import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from sqlalchemy.exc import SQLAlchemyError

from agentflow import __version__
from agentflow.application.orchestration.worker import HandlerRegistry
from agentflow.bootstrap import build_handlers
from agentflow.config import Settings
from agentflow.domain.workflow.spec import WorkflowSpec
from agentflow.infrastructure.persistence.store import (
    ConflictError,
    NotFoundError,
    Store,
)


class BodyLimit:
    """在 JSON 解析前限制请求体，包括未提供 Content-Length 的分块请求。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            return await self.app(scope, receive, send)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > 2_000_000:
                return await JSONResponse({"detail": "request body exceeds 2 MB"}, 413)(
                    scope, receive, send
                )
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


class SubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow_id: str
    inputs: dict[str, JsonValue] = Field(default_factory=dict)
    submission_key: str | None = Field(default=None, min_length=1, max_length=128)


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approve: bool
    actor: str = Field(min_length=1, max_length=80)
    reason: str = Field(default="", max_length=1000)


def create_app(
    settings: Settings | None = None,
    store: Store | None = None,
    handlers: HandlerRegistry | None = None,
) -> FastAPI:
    settings = settings or Settings()
    if settings.api_token is None or len(settings.api_token.get_secret_value()) < 16:
        raise ValueError(
            "AGENTFLOW_API_TOKEN must be configured with at least 16 characters"
        )
    owned_store = store is None
    store = store or Store(settings.database_url.get_secret_value())
    handlers = handlers if handlers is not None else build_handlers(settings)

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            if owned_store:
                store.close()

    app = FastAPI(title="AgentFlow", version=__version__, lifespan=lifespan)
    app.state.store = store
    app.add_middleware(BodyLimit)
    bearer = HTTPBearer(auto_error=False)

    def authorize(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ):
        expected = settings.api_token.get_secret_value().encode()
        if credentials is None or not secrets.compare_digest(
            credentials.credentials.encode(), expected
        ):
            raise HTTPException(
                401, "unauthorized", headers={"WWW-Authenticate": "Bearer"}
            )

    app.state.authorize = authorize
    auth = [Depends(authorize)]

    @app.exception_handler(NotFoundError)
    async def missing(_request, _error):
        return JSONResponse({"detail": "resource not found"}, 404)

    @app.exception_handler(ConflictError)
    async def conflict(_request, error):
        return JSONResponse({"detail": str(error)}, 409)

    @app.exception_handler(ValueError)
    async def invalid(_request, _error):
        return JSONResponse({"detail": "invalid request"}, 422)

    @app.exception_handler(SQLAlchemyError)
    async def database_unavailable(_request, _error):
        return JSONResponse({"detail": "database unavailable"}, 503)

    @app.get("/health")
    def health():
        store.health()
        return {"status": "ok"}

    @app.get("/api/meta", dependencies=auth)
    def meta():
        return {
            "provider": settings.provider,
            "handlers": handlers.names(),
            "version": __version__,
        }

    @app.get("/api/workflows", dependencies=auth)
    def workflows():
        return store.list_workflows()

    @app.post("/api/workflows", dependencies=auth, status_code=201)
    def register(spec: WorkflowSpec):
        if any(step.handler not in handlers.names() for step in spec.steps):
            raise HTTPException(422, "unknown handler")
        return {"id": store.register(spec)}

    @app.get("/api/runs", dependencies=auth)
    def runs(limit: int = Query(100, ge=1, le=200)):
        return store.list_runs(limit)

    @app.post("/api/runs", dependencies=auth, status_code=201)
    def submit(body: SubmitRequest):
        return {"id": store.submit(body.workflow_id, body.inputs, body.submission_key)}

    @app.get("/api/runs/{run_id}", dependencies=auth)
    def detail(run_id: str):
        return {
            **store.get_run(run_id),
            "approvals": store.approvals(run_id),
            "artifacts": store.artifacts(run_id),
        }

    @app.post("/api/runs/{run_id}/cancel", dependencies=auth)
    def cancel(run_id: str):
        store.cancel(run_id)
        return {"status": store.get_run(run_id)["status"]}

    @app.post("/api/approvals/{approval_id}", dependencies=auth)
    def approve(approval_id: str, body: DecisionRequest):
        store.decide(approval_id, body.approve, body.actor, body.reason)
        return {"status": "approved" if body.approve else "rejected"}

    @app.get("/api/runs/{run_id}/events", dependencies=auth)
    async def events(
        run_id: str,
        request: Request,
        after: int = Query(0, ge=0),
        follow: bool = True,
        last_event_id: str | None = Header(default=None),
    ):
        await asyncio.to_thread(store.get_run, run_id)
        if last_event_id is not None:
            try:
                after = max(after, int(last_event_id))
            except ValueError:
                raise HTTPException(422, "invalid Last-Event-ID") from None

        async def stream():
            cursor = after
            while not await request.is_disconnected():
                batch = await asyncio.to_thread(store.events, run_id, cursor)
                for item in batch:
                    cursor = item["id"]
                    yield f"id: {cursor}\nevent: update\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
                if len(batch) == 100:
                    continue
                status = (await asyncio.to_thread(store.get_run, run_id))["status"]
                if not follow:
                    return
                if status in {"succeeded", "failed", "cancelled"}:
                    # 最后一批查询与读取终态之间可能刚好发生提交；先补发尾部事件。
                    if await asyncio.to_thread(store.events, run_id, cursor):
                        continue
                    return
                yield ": heartbeat\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    @app.websocket("/api/runs/{run_id}/ws")
    async def websocket_events(socket: WebSocket, run_id: str):
        """可选 WebSocket 接口：首帧认证，token 不出现在 URL 或访问日志中。"""
        await socket.accept()
        try:
            raw = await asyncio.wait_for(socket.receive_text(), 5)
            if len(raw) > 4096:
                await socket.close(code=1008)
                return
            credentials = json.loads(raw)
            if not isinstance(credentials, dict):
                raise ValueError("invalid authentication frame")
            supplied = credentials.get("token", "")
            cursor = credentials.get("after", 0)
            if (
                not isinstance(supplied, str)
                or not isinstance(cursor, int)
                or cursor < 0
                or not secrets.compare_digest(
                    supplied.encode(), settings.api_token.get_secret_value().encode()
                )
            ):
                await socket.close(code=1008)
                return
            await asyncio.to_thread(store.get_run, run_id)
            while True:
                batch = await asyncio.to_thread(store.events, run_id, cursor)
                for item in batch:
                    cursor = item["id"]
                    await socket.send_json(item)
                if len(batch) == 100:
                    continue
                status = (await asyncio.to_thread(store.get_run, run_id))["status"]
                if status in {"succeeded", "failed", "cancelled"}:
                    if await asyncio.to_thread(store.events, run_id, cursor):
                        continue
                    await socket.close(code=1000)
                    return
                await socket.send_json({"kind": "heartbeat", "after": cursor})
                await asyncio.sleep(1)
        except WebSocketDisconnect:
            return
        except (ValueError, TimeoutError, NotFoundError):
            await socket.close(code=1008)

    if settings.telemetry:
        from agentflow.infrastructure.telemetry.metrics import install_metrics

        install_metrics(app, auth)
    return app
