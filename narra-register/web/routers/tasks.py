import os
# -*- coding: utf-8 -*-
import json
import time
import threading
from core.yyds_mail import YYDSMailClient
from core.narra_auth import NarraNexusClient
from core.engine import RegistrationEngine
from fastapi import APIRouter, Depends, Request
from web.shared import db, task_lock, task_state, add_log, authenticate_admin

router = APIRouter(tags=['批量注册任务'])

@router.get("/api/task/status")
async def get_task_status(user: str = Depends(authenticate_admin)):
    with task_lock:
        st = task_state["status"]
        can_resume = (st in ["paused", "idle"]) and (task_state["target_count"] > task_state["completed_count"]) and (task_state["completed_count"] > 0)
        return {
            "status": "success",
            "data": {
                "status": task_state["status"],
                "total": task_state["total"],
                "current": task_state["current"],
                "success_count": task_state["success_count"],
                "fail_count": task_state["fail_count"],
                "target_count": task_state["target_count"],
                "completed_count": task_state["completed_count"],
                "can_resume": can_resume,
                "logs": list(task_state["logs"])
            }
        }

@router.post("/api/task/clear-logs")
@router.post("/api/task/logs/clear")
async def clear_task_logs(user: str = Depends(authenticate_admin)):
    with task_lock:
        task_state["logs"].clear()
    return {"status": "success", "message": "日志已清空"}

def _run_task_worker(target_total: int, completed_start: int, interval: int, domain_strat: str, code_timeout: int):
    with task_lock:
        engine = task_state["engine"]
    
    current_count = completed_start
    succ_count = task_state["success_count"]
    fail_count = task_state["fail_count"]
    
    for i in range(completed_start + 1, target_total + 1):
        with task_lock:
            if task_state["stop_flag"] or (engine and engine._is_stopped):
                task_state["status"] = "paused"
                task_state["completed_count"] = current_count
                add_log(f"⏸ 任务已暂停: 已完成 {current_count}/{target_total} 个 (成功: {succ_count}, 失败: {fail_count})")
                return
            task_state["current"] = i

        add_log(f"▶ 正在执行第 {i}/{target_total} 个账号注册 ...")
        
        ok, msg, acc = engine.register_single_auto(
            domain_strategy=domain_strat,
            timeout_seconds=code_timeout,
            log_callback=add_log
        )
        
        with task_lock:
            if ok:
                succ_count += 1
                task_state["success_count"] = succ_count
                email = acc["email"] if acc else "unknown"
                add_log(f"✅ 第 {i} 个注册成功: {email}")
            else:
                fail_count += 1
                task_state["fail_count"] = fail_count
                add_log(f"❌ 注册失败: {msg}")
            
            current_count = i
            task_state["completed_count"] = current_count

        if i < target_total:
            for _ in range(interval):
                with task_lock:
                    if task_state["stop_flag"] or (engine and engine._is_stopped):
                        break
                time.sleep(1)

    with task_lock:
        task_state["status"] = "idle"
        task_state["completed_count"] = current_count
        add_log(f"🎉 批量注册任务执行完毕! 目标: {target_total}, 成功: {succ_count}, 失败: {fail_count}")

@router.post("/api/task/start")
async def start_task(request: Request, user: str = Depends(authenticate_admin)):
    try:
        body = await request.json()
    except Exception:
        body = {}
        
    count = int(body.get("count") or body.get("target_count") or 5)
    interval = int(body.get("interval_seconds") or 2)
    domain_strat = str(body.get("domain_strategy") or "smart")
    is_resume = bool(body.get("is_resume") or False)
    
    # 支持自定义验证码轮询超时时间
    code_timeout = int(body.get("code_timeout") or body.get("verification_code_timeout") or db.get_setting("verification_code_timeout", "60"))
    
    with task_lock:
        if task_state["status"] == "running":
            return {"status": "error", "message": "已有任务正在执行中"}
        
        latest_key = db.get_setting("yyds_mail_api_key", os.environ.get("YYDS_KEY", ""))
        latest_proxy = db.get_setting("proxy_url", "http://clash-proxy:7890")
        
        yyds = YYDSMailClient(api_key=latest_key)
        narra = NarraNexusClient(proxy=latest_proxy)
        engine = RegistrationEngine(
            db=db,
            yyds_client=yyds,
            narra_client=narra,
            proxy=latest_proxy,
            default_code_timeout=code_timeout
        )
        
        task_state["engine"] = engine
        task_state["stop_flag"] = False
        task_state["interval_seconds"] = interval
        task_state["domain_strategy"] = domain_strat
        
        if is_resume and task_state["target_count"] > 0:
            target_total = task_state["target_count"]
            completed_start = task_state["completed_count"]
            task_state["status"] = "running"
            add_log(f"▶ 继续执行剩余注册任务: 已完成 {completed_start} / 目标 {target_total} 个 (验证码超时: {code_timeout}s)")
        else:
            target_total = count
            completed_start = 0
            task_state["status"] = "running"
            task_state["target_count"] = count
            task_state["completed_count"] = 0
            task_state["total"] = count
            task_state["current"] = 0
            task_state["success_count"] = 0
            task_state["fail_count"] = 0
            add_log(f"🚀 开始批量注册任务: 目标 {count} 个, 间隔 {interval}s, 验证码超时 {code_timeout}s")
        
        worker_thread = threading.Thread(
            target=_run_task_worker,
            args=(target_total, completed_start, interval, domain_strat, code_timeout),
            daemon=True
        )
        task_state["thread"] = worker_thread
        worker_thread.start()
        
        return {"status": "success", "message": "注册任务已启动"}

@router.post("/api/task/stop")
async def stop_task(user: str = Depends(authenticate_admin)):
    with task_lock:
        if task_state["status"] != "running":
            return {"status": "error", "message": "暂无运行中的任务"}
        task_state["stop_flag"] = True
        if task_state.get("engine"):
            task_state["engine"].stop()
        add_log("🛑 正在停止/暂停当前批量任务...")
    return {"status": "success", "message": "已发送停止/暂停信号"}