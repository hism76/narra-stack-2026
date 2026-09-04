import os
import asyncio
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from web.shared import db, pool_manager
from core.async_gateway import router as gateway_router, pool as gw_pool, db_gw as gw_db

app = FastAPI(title="NarraNexus Management & Gateway")

# 挂载 OpenAI 与 Responses API 网关路由
app.include_router(gateway_router)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
if not os.path.exists(STATIC_DIR):
    os.makedirs(STATIC_DIR, exist_ok=True)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.middleware("http")
async def _no_cache_middleware(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp

# 挂载精简后的管理路由
from web.routers.console import router as console_router
from web.routers.health_check import router as healthcheck_router
from web.routers.accounts import router as accounts_router
from web.routers.settings import router as settings_router
from web.routers.tasks import router as tasks_router
from web.routers.gateway import router as gateway_mgmt_router
from web.routers.logging import router as logging_router

app.include_router(console_router)
app.include_router(healthcheck_router)
app.include_router(accounts_router)
app.include_router(settings_router)
app.include_router(tasks_router)
app.include_router(gateway_mgmt_router)
app.include_router(logging_router)