# -*- coding: utf-8 -*-
import os
import time
import secrets
import threading
from typing import Any, Dict
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from core.database import AccountDatabase
from core.pool_manager import AccountPoolManager
from core.dynlog import dynlog
import logging as _logging

dynlog.attach("web", _logging.getLogger("web"))
dynlog.attach("app", _logging.getLogger("app"))
dynlog.attach("uvicorn.access", _logging.getLogger("uvicorn.access"))

db = AccountDatabase()
pool_manager = AccountPoolManager(db=db)
security = HTTPBasic()

# 批量注册任务状态
task_lock = threading.RLock()
task_state = {
    "status": "idle",
    "total": 0,
    "current": 0,
    "success_count": 0,
    "fail_count": 0,
    "target_count": 0,
    "completed_count": 0,
    "interval_seconds": 2,
    "domain_strategy": "smart",
    "logs": [],
    "engine": None,
    "thread": None,
    "stop_flag": False,
}

# 全池健康状态摘要
health_check_lock = threading.Lock()
health_check_state = {
    "is_running": False,
    "total": 0,
    "checked": 0,
    "healthy_count": 0,
    "cooling_new_count": 0,
    "cooling_kept_count": 0,
    "revived_count": 0,
    "invalid_count": 0,
    "timeout_count": 0,
    "current_email": "",
    "message": "就绪",
    "start_time": 0,
    "end_time": 0
}

def add_log(msg: str):
    with task_lock:
        timestamp = time.strftime("%H:%M:%S")
        log_entry = f"[{timestamp}] {msg}"
        task_state["logs"].append(log_entry)
        if len(task_state["logs"]) > 300:
            task_state["logs"].pop(0)

def authenticate_admin(credentials: HTTPBasicCredentials = Depends(security)):
    admin_user = db.get_setting("admin_username", os.environ.get("ADMIN_USER", "admin"))
    admin_pass = db.get_setting("admin_password", os.environ.get("ADMIN_PASS", "admin123456"))
    
    is_correct_username = secrets.compare_digest(credentials.username, admin_user)
    is_correct_password = secrets.compare_digest(credentials.password, admin_pass)
    
    if not (is_correct_username and is_correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "web", "templates"))
class DummyQuotaWorker: pass
quota_worker = DummyQuotaWorker()
