"""Composition root for the reference application API and worker."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends

from agentflow.bootstrap import build_handlers as build_core_handlers
from agentflow.bootstrap import build_provider
from agentflow.config import Settings
from agentflow.interfaces.api.app import create_app as create_core_app
from agentflow.interfaces.cli.main import main as core_main
from applications.living_guideline.api import router
from applications.living_guideline.handlers import GuidelineHandlers
from applications.living_guideline.workflow import GUIDELINE


def build_handlers(settings, tools=None):
    handlers = build_core_handlers(settings, tools)
    guideline = GuidelineHandlers(build_provider(settings))
    handlers.register("guideline.extract", guideline.extract)
    handlers.register("guideline.draft", guideline.draft)
    handlers.register("guideline.publish", guideline.publish)
    return handlers


def create_app(settings=None, store=None):
    settings = settings or Settings()
    app = create_core_app(settings, store, build_handlers(settings))
    core_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        async with core_lifespan(app):
            app.state.guideline_id = await asyncio.to_thread(
                app.state.store.register, GUIDELINE
            )
            yield

    app.router.lifespan_context = lifespan
    app.include_router(router, dependencies=[Depends(app.state.authorize)])
    return app


def main():
    core_main(
        api_factory="applications.living_guideline.bootstrap:create_app",
        handler_factory=build_handlers,
    )


if __name__ == "__main__":
    main()
