from fastapi import FastAPI

from alpha_bot.delivery.web.routes import router


def create_app() -> FastAPI:
    app = FastAPI(title="Alpha Bot Dashboard", version="0.1.0")
    app.include_router(router)
    return app
