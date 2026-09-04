# -*- coding: utf-8 -*-
# 拆分自 web/app.py (refactor #4, 行为等价, 见 git 历史). 域: 日志配置.
import json
from fastapi import APIRouter, Depends, Request
from web.shared import db, add_log, authenticate_admin

router = APIRouter(tags=['日志配置'])

@router.get("/api/logging")
async def get_logging(user: str = Depends(authenticate_admin)):
    from core.dynlog import dynlog, get_dynlevel
    return {"status": "success", "data": {
        "log_enabled": db.get_setting("log_enabled", "true") != "false",
        "log_level": get_dynlevel() or db.get_setting("log_level", "INFO"),
        "available_levels": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        "scopes": dynlog.scopes,
    }}


@router.post("/api/logging")
async def save_logging(request: Request, user: str = Depends(authenticate_admin)):
    from core.dynlog import dynlog
    body = await request.json()
    enabled = bool(body.get("log_enabled", True))
    level = str(body.get("log_level", "INFO")).upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        level = "INFO"
    db.set_setting("log_enabled", "true" if enabled else "false")
    db.set_setting("log_level", level)
    dynlog.apply(enabled, level)
    add_log(f"[Logging] 日志配置已热更新: enabled={enabled}, level={level}")
    return {"status": "success", "message": f"日志已实时更新: 开关={'开' if enabled else '关'}, 级别={level}"}

