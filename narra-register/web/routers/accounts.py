# -*- coding: utf-8 -*-
# 拆分自 web/app.py (refactor #4, 行为等价, 见 git 历史). 域: 账号管理.
import requests
import csv
import io
import json
import time
from fastapi.responses import StreamingResponse
from core.pool_manager import is_token_valid
from fastapi import APIRouter, Depends, Request
from web.shared import db, pool_manager, authenticate_admin
from core.async_gateway import pool as gw_pool

router = APIRouter(tags=['账号管理'])

@router.get("/api/accounts")
def get_accounts(
    page: int = 1, 
    page_size: int = 50, 
    search: str = "", 
    status_filter: str = "all",
    _user: str = Depends(authenticate_admin)
):
    try:
        accounts, total = db.search_accounts(
            page=page, 
            page_size=page_size, 
            search=search, 
            status_filter=status_filter
        )
        return {
            "status": "success",
            "data": {
                "accounts": accounts,
                "total": total,
                "page": page,
                "page_size": page_size
            }
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.post("/api/accounts/reset-cooling")
async def reset_cooling_accounts(user: str = Depends(authenticate_admin)):
    """一键唤醒所有冷却中的账号"""
    try:
        count = db.reset_all_cooling_accounts()
        return {"status": "success", "message": f"成功唤醒 {count} 个处于冷却中的账号"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.post("/api/accounts/test-single")
async def test_single_account(request: Request, user: str = Depends(authenticate_admin)):
    """NarraNexus 单账号在线测活与额度刷新探针"""
    try:
        body = await request.json()
        email = body.get("email")
        if not email:
            return {"status": "error", "message": "缺少账号邮箱参数"}
        with db._get_conn() as conn:
            acc = conn.execute(
                "SELECT email, password, access_token, refresh_token, user_id, status FROM accounts WHERE email = ?",
                (email,)
            ).fetchone()
            if not acc:
                return {"status": "error", "message": "数据库中未找到该账号"}
            token = acc["access_token"]
            user_id = acc["user_id"]
            refresh_token = acc["refresh_token"]

        from core.narra_auth import NarraNexusClient
        client = NarraNexusClient()
        t0 = time.time()
        quota_info = client.get_quota(token, user_id)
        latency = round((time.time() - t0) * 1000)

        if quota_info and quota_info.get("status") == "active":
            rem = quota_info.get("remaining", 3.0)
            db.mark_account_active(email)
            return {"status": "success", "message": f"账号状态正常 🟢 可用额度: ${rem} (响应耗时 {latency}ms)"}
        
        # 若凭据失效，尝试通过 NetMind loginToken 自动静默换票刷新
        if refresh_token:
            try:
                refreshed = client.exchange_narra_token(refresh_token)
                new_jwt = refreshed.get("token")
                if new_jwt:
                    db.mark_account_active(email, new_token=new_jwt)
                    return {"status": "success", "message": f"账号凭证已自动复活刷新 🟢 (响应耗时 {latency}ms)"}
            except Exception:
                pass

        err_detail = quota_info.get("detail") if quota_info else "请求无有效响应"
        db.mark_account_cooling(email, reason=str(err_detail))
        return {"status": "warn", "message": f"账号响应异常: {err_detail} (已标记冷却)"}
    except Exception as e:
        return {"status": "error", "message": f"探针检测异常: {str(e)}"}

@router.post("/api/accounts/batch-action")
async def batch_accounts_action(request: Request, user: str = Depends(authenticate_admin)):
    """表格多选批量操作（批量删除 / 批量唤醒），联动清理内存缓存"""
    body = await request.json()
    action = body.get("action")
    identifiers = body.get("identifiers") or []
    if not identifiers:
        return {"status": "error", "message": "未选择任何账号"}

    if action == "delete":
        deleted, deleted_emails = db.batch_delete_accounts(identifiers)
        for em in deleted_emails:
            gw_pool.invalidate_cached(em)
        return {"status": "success", "message": f"成功批量删除 {deleted} 个账号"}
    elif action == "reset_cooling":
        reset_count = db.batch_reset_cooling(identifiers)
        return {"status": "success", "message": f"成功唤醒 {reset_count} 个冷却账号"}
    else:
        return {"status": "error", "message": f"未知的批量操作: {action}"}

@router.get("/api/accounts/export")
def export_accounts_csv(user: str = Depends(authenticate_admin)):
    """一键导出 CSV 数据"""
    all_accs = db.get_all_accounts_for_export()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "邮箱", "密码", "状态", "冷却截至时间", "最后失败原因", "成功调用次数", "失败次数", "创建时间", "最后更新时间"])
    for a in all_accs:
        writer.writerow([
            a.get("id"), a.get("email"), a.get("password"), a.get("status"), 
            a.get("cooldown_until") or "", a.get("fail_reason") or "",
            a.get("success_count") or 0, a.get("fail_count") or 0,
            a.get("created_at") or "", a.get("updated_at") or ""
        ])
    output.seek(0)
    filename = f"omnibot_accounts_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

@router.post("/api/accounts/import")
async def import_accounts_text(request: Request, user: str = Depends(authenticate_admin)):
    """多行文本批量导入/增量入库"""
    body = await request.json()
    raw_text = body.get("content", "").strip()
    if not raw_text:
        return {"status": "error", "message": "导入内容不能为空"}

    lines = raw_text.splitlines()
    items_to_import = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "----" in line:
            parts = line.split("----", 1)
        elif "," in line:
            parts = line.split(",", 1)
        elif ":" in line:
            parts = line.split(":", 1)
        elif "\t" in line:
            parts = line.split("\t", 1)
        else:
            parts = line.split(None, 1)

        email = parts[0].strip()
        password = parts[1].strip() if len(parts) > 1 and parts[1].strip() else db.get_setting("default_account_password", "Omni#2026x")
        if email and "@" in email:
            items_to_import.append({"email": email, "password": password})

    if not items_to_import:
        return {"status": "error", "message": "未解析出合法的邮箱账号数据"}

    res = db.import_accounts(items_to_import)
    return {
        "status": "success",
        "message": f"导入完成！新增 {res['added']} 个，更新 {res['updated']} 个，共处理 {res['total']} 个账号",
        "data": res
    }

@router.delete("/api/accounts/{account_id}")
async def delete_account_by_id(account_id: str, user: str = Depends(authenticate_admin)):
    """删除单账号，联动清除内存 Token 缓存"""
    success, email_to_del = db.delete_account(account_id)
    if success:
        if email_to_del:
            gw_pool.invalidate_cached(email_to_del)
        return {"status": "success", "message": "账号删除成功"}
    return {"status": "error", "message": "未找到对应账号或删除失败"}

