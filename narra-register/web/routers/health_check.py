# -*- coding: utf-8 -*-
# 拆分自 web/app.py (refactor #4, 行为等价, 见 git 历史). 域: 全池体检.
import requests
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from core.pool_manager import is_token_valid
from fastapi import APIRouter, Depends, Request
from web.shared import db, pool_manager, health_check_lock, health_check_state, authenticate_admin
from core.async_gateway import pool as gw_pool

router = APIRouter(tags=['全池体检'])

# 待体检账号快照与游标
_hc_accounts = []
_hc_cursor = 0

def check_single_account(acc):
    """检测单个账号的健康状态 (极轻量 1-token 探针)"""
    email = acc["email"]
    pwd = acc.get("password") or db.get_setting("default_account_password", "Omni#2026x")
    orig_token = acc.get("access_token")
    orig_status = acc.get("status", "active")
    orig_cooldown = acc.get("cooldown_until")

    # 1. 确保拿到可用 Token (优先新鲜缓存 > 新鲜DB > 重登)
    tok = None
    cached = gw_pool._tokens.get(email)
    if cached and cached.get("token") and is_token_valid(cached["token"]):
        tok = cached["token"]
    elif orig_token and is_token_valid(orig_token):
        tok = orig_token

    if not tok:
        tok = pool_manager._login_account(email, pwd)
        if tok:
            gw_pool.seed_cached(email, tok, pwd)
            db.update_token(email, tok)

    if not tok:
        res_action = db.record_health_check_result(
            email=email,
            is_healthy=False,
            status_code=401,
            orig_status=orig_status,
            orig_cooldown_until=orig_cooldown,
            err_msg="Login failed (no token)"
        )
        gw_pool.invalidate_cached(email)
        return res_action, email

    # 2. 发送 1-token 极轻量探针
    hdrs = {
        "Authorization": f"Bearer {tok}",
        "Content-Type": "application/json",
        "User-Agent": "OmniBot-Android/2.0"
    }
    payload = {
        "model": "Qwen3.5-Plus",
        "messages": [{"role": "user", "content": "hi"}],
        "enable_thinking": False,
        "max_tokens": 1,
        "stream": False
    }

    try:
        r = requests.post("https://model-api.omnimind.com.cn/v1/chat/completions", json=payload, headers=hdrs, timeout=12)
        if r.status_code == 200:
            res_action = db.record_health_check_result(
                email=email,
                is_healthy=True,
                status_code=200,
                orig_status=orig_status,
                orig_cooldown_until=orig_cooldown,
                new_token=tok
            )
        elif r.status_code == 503 or "quota" in r.text.lower():
            res_action = db.record_health_check_result(
                email=email,
                is_healthy=False,
                status_code=503,
                orig_status=orig_status,
                orig_cooldown_until=orig_cooldown
            )
            gw_pool.invalidate_cached(email)
        elif r.status_code in (401, 403):
            # 尝试静默重登刷新一次
            fresh_tok = pool_manager._login_account(email, pwd)
            if fresh_tok:
                hdrs["Authorization"] = f"Bearer {fresh_tok}"
                r2 = requests.post("https://model-api.omnimind.com.cn/v1/chat/completions", json=payload, headers=hdrs, timeout=12)
                if r2.status_code == 200:
                    res_action = db.record_health_check_result(
                        email=email,
                        is_healthy=True,
                        status_code=200,
                        orig_status=orig_status,
                        orig_cooldown_until=orig_cooldown,
                        new_token=fresh_tok
                    )
                    gw_pool.seed_cached(email, fresh_tok, pwd)
                else:
                    res_action = db.record_health_check_result(
                        email=email,
                        is_healthy=False,
                        status_code=r2.status_code,
                        orig_status=orig_status,
                        orig_cooldown_until=orig_cooldown,
                        err_msg=r2.text
                    )
                    gw_pool.invalidate_cached(email)
            else:
                res_action = db.record_health_check_result(
                    email=email,
                    is_healthy=False,
                    status_code=401,
                    orig_status=orig_status,
                    orig_cooldown_until=orig_cooldown,
                    err_msg="Re-login failed"
                )
                gw_pool.invalidate_cached(email)
        else:
            db.record_health_check_result(
                email=email,
                is_healthy=False,
                status_code=r.status_code,
                orig_status=orig_status,
                orig_cooldown_until=orig_cooldown,
                err_msg=f"Unexpected HTTP {r.status_code}: {r.text[:80]}"
            )
            gw_pool.invalidate_cached(email)
            res_action = "error"
    except Exception as e:
        db.record_health_check_result(
            email=email,
            is_healthy=False,
            status_code=0,
            orig_status=orig_status,
            orig_cooldown_until=orig_cooldown,
            err_msg=f"Timeout/exception: {str(e)[:80]}"
        )
        gw_pool.invalidate_cached(email)
        res_action = "timeout"

    time.sleep(0.1)  # 温和间隔防突发风控
    return res_action, email


def _run_health_check_worker(is_resume: bool = False):
    """全池体检核心工作线程 (支持断点继续与即时暂停)"""
    global _hc_accounts, _hc_cursor

    if not is_resume:
        _hc_accounts = db.get_all_accounts_for_health_check()
        _hc_cursor = 0
        total = len(_hc_accounts)
        with health_check_lock:
            health_check_state.update({
                "status": "running",
                "is_running": True,
                "is_paused": False,
                "pause_flag": False,
                "stop_flag": False,
                "total": total,
                "checked": 0,
                "healthy_count": 0,
                "cooling_new_count": 0,
                "cooling_kept_count": 0,
                "revived_count": 0,
                "invalid_count": 0,
                "timeout_count": 0,
                "current_email": "",
                "message": f"正在体检全量 {total} 个账号...",
                "start_time": time.time(),
                "end_time": 0
            })
    else:
        total = len(_hc_accounts)
        with health_check_lock:
            health_check_state["status"] = "running"
            health_check_state["is_running"] = True
            health_check_state["is_paused"] = False
            health_check_state["pause_flag"] = False
            health_check_state["stop_flag"] = False
            health_check_state["message"] = f"正在从断点 ({_hc_cursor}/{total}) 继续体检..."

    # 循环单账号处理并检测暂停/停止信号
    while _hc_cursor < len(_hc_accounts):
        with health_check_lock:
            if health_check_state.get("stop_flag"):
                health_check_state["status"] = "idle"
                health_check_state["is_running"] = False
                health_check_state["is_paused"] = False
                health_check_state["message"] = f"体检任务已停止 (完成 {_hc_cursor}/{len(_hc_accounts)})"
                return
            if health_check_state.get("pause_flag"):
                health_check_state["status"] = "paused"
                health_check_state["is_running"] = False
                health_check_state["is_paused"] = True
                health_check_state["message"] = f"体检已暂停于进度 ({_hc_cursor}/{len(_hc_accounts)})"
                return

        acc = _hc_accounts[_hc_cursor]
        with health_check_lock:
            health_check_state["current_email"] = acc.get("email", "")

        try:
            action, em = check_single_account(acc)
            with health_check_lock:
                _hc_cursor += 1
                health_check_state["checked"] = _hc_cursor
                if action == "healthy":
                    health_check_state["healthy_count"] += 1
                elif action == "revived":
                    health_check_state["revived_count"] += 1
                    health_check_state["healthy_count"] += 1
                elif action == "cooling_new":
                    health_check_state["cooling_new_count"] += 1
                elif action == "cooling_kept":
                    health_check_state["cooling_kept_count"] += 1
                elif action == "invalid":
                    health_check_state["invalid_count"] += 1
                elif action in ("timeout", "error"):
                    health_check_state["timeout_count"] = health_check_state.get("timeout_count", 0) + 1
        except Exception as e:
            with health_check_lock:
                _hc_cursor += 1
                health_check_state["checked"] = _hc_cursor

    # 全部执行完毕
    with health_check_lock:
        health_check_state["status"] = "idle"
        health_check_state["is_running"] = False
        health_check_state["is_paused"] = False
        health_check_state["end_time"] = time.time()
        elapsed = round(health_check_state["end_time"] - health_check_state.get("start_time", time.time()), 1)
        health_check_state["message"] = (
            f"全池体检完成！耗时 {elapsed}s | 正常就绪 {health_check_state['healthy_count']} 个 (含提前复活 {health_check_state['revived_count']} 个) | 新进冷却 {health_check_state['cooling_new_count']} 个 | 保持冷却 {health_check_state['cooling_kept_count']} 个 | 失效 {health_check_state['invalid_count']} 个"
        )


@router.post("/api/accounts/health-check")
async def handle_health_check_action(request: Request, user: str = Depends(authenticate_admin)):
    """全池体检控制接口 (支持 start / pause / resume / stop)"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    action = body.get("action", "start")

    with health_check_lock:
        st = health_check_state.get("status", "idle")
        is_run = health_check_state.get("is_running", False)

        if action == "start":
            if is_run:
                return {"status": "error", "message": "全池体检正在运行中"}
            health_check_state["is_running"] = True
            health_check_state["status"] = "running"
            t = threading.Thread(target=_run_health_check_worker, kwargs={"is_resume": False}, daemon=True)
            t.start()
            return {"status": "success", "message": "全池体检已启动"}

        elif action == "pause":
            if not is_run:
                return {"status": "error", "message": "体检未在运行，无法暂停"}
            health_check_state["pause_flag"] = True
            return {"status": "success", "message": "已发送暂停信号，当前账号检测完毕后即刻暂停"}

        elif action == "resume":
            if is_run:
                return {"status": "error", "message": "体检已在运行中"}
            if not _hc_accounts or _hc_cursor >= len(_hc_accounts):
                # 若已无未完成账号，则重新从头开始
                t = threading.Thread(target=_run_health_check_worker, kwargs={"is_resume": False}, daemon=True)
                t.start()
                return {"status": "success", "message": "重新启动全池体检"}
            t = threading.Thread(target=_run_health_check_worker, kwargs={"is_resume": True}, daemon=True)
            t.start()
            return {"status": "success", "message": f"已从断点 ({_hc_cursor}/{len(_hc_accounts)}) 继续体检"}

        elif action == "stop":
            health_check_state["stop_flag"] = True
            health_check_state["pause_flag"] = False
            return {"status": "success", "message": "体检停止信号已发送"}

        else:
            return {"status": "error", "message": f"未知的体检动作: {action}"}


@router.post("/api/accounts/health-check/pause")
async def pause_health_check(user: str = Depends(authenticate_admin)):
    return await handle_health_check_action(Request(scope={"type": "http"}), user=user)


@router.get("/api/accounts/health-check/status")
async def get_health_check_status(user: str = Depends(authenticate_admin)):
    with health_check_lock:
        return {"status": "success", "data": dict(health_check_state)}
