# -*- coding: utf-8 -*-
import json
import time
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi import APIRouter, Depends, Request
from web.shared import db, add_log, authenticate_admin, templates
from core.async_gateway import pool as gw_pool, db_gw as gw_db, keys_store as gw_keys_store

router = APIRouter(tags=['控制台'])

@router.get("/api/keys")
async def list_api_keys(user: str = Depends(authenticate_admin)):
    raw = gw_db.get_setting("api_keys", "[]")
    usage_raw = gw_db.get_setting("api_keys_usage", "{}")
    try:
        keys = json.loads(raw)
    except Exception:
        keys = []
    try:
        usage = json.loads(usage_raw)
    except Exception:
        usage = {}
    for k in keys:
        bare = str(k.get("key", "")).removeprefix("sk-")
        u = usage.get(bare, {})
        k["requests"] = u.get("requests", 0)
        k["success"] = u.get("success", 0)
        k["fail"] = u.get("fail", 0)
        k["last_used"] = u.get("last_used", 0)
    return {"keys": keys}

@router.post("/api/keys")
async def save_api_keys(request: Request, user: str = Depends(authenticate_admin)):
    body = await request.json()
    items = body.get("keys")
    if not isinstance(items, list):
        return {"status": "error", "message": "keys 必须为数组"}
    cleaned = []
    for it in items:
        key = str(it.get("key", "")).strip()
        if not key:
            continue
        cleaned.append({
            "key": key,
            "name": str(it.get("name", "unnamed"))[:40],
            "rpm": int(it.get("rpm", 0) or 0)
        })
    gw_db.set_setting("api_keys", json.dumps(cleaned, ensure_ascii=False))
    gw_keys_store.reload()
    add_log(f"[Gateway] API Keys 已更新，当前 {len(cleaned)} 个")
    return {"status": "success", "count": len(cleaned)}

@router.get("/api/metrics")
async def gateway_metrics(user: str = Depends(authenticate_admin)):
    raw = gw_db.get_setting("api_keys", "[]")
    usage_raw = gw_db.get_setting("api_keys_usage", "{}")
    try:
        keys = json.loads(raw)
    except Exception:
        keys = []
    try:
        usage = json.loads(usage_raw)
    except Exception:
        usage = {}
    key_metrics = []
    for k in keys:
        bare = str(k.get("key", "")).removeprefix("sk-")
        u = usage.get(bare, {})
        key_metrics.append({
            "key": bare[:6] + "..." if len(bare) > 6 else bare,
            "name": k.get("name", "unnamed"),
            "rpm_limit": int(k.get("rpm", 0) or 0),
            "requests": u.get("requests", 0),
            "success": u.get("success", 0),
            "fail": u.get("fail", 0),
            "last_used": u.get("last_used", 0),
        })
    return {
        "gateway": {
            "cached_tokens": 0,
            "fresh_tokens": 0,
            "circuit_breaker": "closed",
        },
        "keys": key_metrics,
        "keys_count": len(keys),
    }

@router.get("/", response_class=HTMLResponse)
async def serve_dashboard(request: Request, user: str = Depends(authenticate_admin)):
    return templates.TemplateResponse(request=request, name="index.html", context={"user": user})

@router.get("/api/status")
@router.get("/api/pool/status")
async def get_dashboard_stats(user: str = Depends(authenticate_admin)):
    """返回全量号池核心统计状态，支持多种字段兼容"""
    stats = db.get_pool_status_summary()
    total = stats.get("total_accounts", 0)
    active = stats.get("active_accounts", 0)
    cooling = stats.get("cooling_accounts", 0)
    invalid = stats.get("invalid_accounts", 0)
    
    # 注入标准短名与完整名，保障所有客户端/前端无缝识别
    data = {
        "total_accounts": total,
        "active_accounts": active,
        "cooling_accounts": cooling,
        "invalid_accounts": invalid,
        "total": total,
        "active": active,
        "cooling": cooling,
        "invalid": invalid,
        "models_count": 91
    }
    return {"status": "success", "data": data}

@router.get("/api/stream/events")
async def stream_dashboard_events(request: Request, user: str = Depends(authenticate_admin)):
    """控制台 SSE 实时状态事件流"""
    import asyncio
    async def event_generator():
        while True:
            if await request.is_disconnected():
                break
            try:
                stats = db.get_pool_status_summary()
                from web.shared import task_state, task_lock
                with task_lock:
                    tk = {
                        "status": task_state.get("status"),
                        "current": task_state.get("current"),
                        "target_count": task_state.get("target_count"),
                        "success_count": task_state.get("success_count"),
                        "fail_count": task_state.get("fail_count"),
                        "last_log": task_state.get("logs", [])[-1] if task_state.get("logs") else "",
                    }
                payload = {
                    "timestamp": time.time(),
                    "pool": {
                        "total": stats.get("total_accounts", 0),
                        "active": stats.get("active_accounts", 0),
                        "cooling": stats.get("cooling_accounts", 0),
                        "invalid": stats.get("invalid_accounts", 0)
                    },
                    "task": tk
                }
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            except Exception:
                pass
            await asyncio.sleep(2.0)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )