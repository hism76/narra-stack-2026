import os
# -*- coding: utf-8 -*-
import json
from fastapi import APIRouter, Depends, Request
from web.shared import db, authenticate_admin

router = APIRouter(tags=['系统设置'])

@router.get("/api/settings")
async def get_settings(user: str = Depends(authenticate_admin)):
    """获取精简后的 NarraNexus 核心系统设置"""
    settings_data = {
        "yyds_mail_api_key": db.get_setting("yyds_mail_api_key", os.environ.get("YYDS_KEY", "")),
        "proxy_url": db.get_setting("proxy_url", "http://clash-proxy:7890"),
        "verification_code_timeout": db.get_setting("verification_code_timeout", "60"),
        "default_reasoning_effort": db.get_setting("default_reasoning_effort", "medium"),
        "fast_mode_enabled": db.get_setting("fast_mode_enabled", "true"),
        "admin_username": db.get_setting("admin_username", os.environ.get("ADMIN_USER", "admin")),
    }
    return {"status": "success", "data": settings_data}

@router.post("/api/settings")
async def update_settings(request: Request, user: str = Depends(authenticate_admin)):
    """更新核心系统设置"""
    body = await request.json()
    allowed_keys = [
        "yyds_mail_api_key",
        "proxy_url",
        "verification_code_timeout",
        "default_reasoning_effort",
        "fast_mode_enabled",
        "admin_username",
        "admin_password",
    ]
    for key in allowed_keys:
        if key in body:
            val = str(body[key]).strip() if body[key] is not None else ""
            if key == "admin_password" and not val:
                continue
            db.set_setting(key, val)
    return {"status": "success", "message": "配置更新成功"}

@router.post("/api/settings/test-yyds")
async def test_yyds(request: Request, user: str = Depends(authenticate_admin)):
    """测试 YYDS 接码连通性"""
    from core.yyds_mail import YYDSMailClient
    body = await request.json()
    api_key = body.get("api_key") or db.get_setting("yyds_mail_api_key", os.environ.get("YYDS_KEY", ""))
    client = YYDSMailClient(api_key=api_key)
    try:
        data = client.create_account()
        return {"status": "success", "message": f"连通成功，获取到临时邮箱: {data.get('address')}"}
    except Exception as e:
        return {"status": "error", "message": f"连接失败: {str(e)}"}