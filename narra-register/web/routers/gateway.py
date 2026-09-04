# -*- coding: utf-8 -*-
# 拆分自 web/app.py (refactor #4, 行为等价, 见 git 历史). 域: 网关参数.
import json
from fastapi import APIRouter, Depends, Request
from web.shared import add_log, authenticate_admin
from core.async_gateway import pool as gw_pool, db_gw as gw_db

router = APIRouter(tags=['网关参数'])

@router.get("/api/gw-params")
async def get_gw_params(user: str = Depends(authenticate_admin)):
    return {"status": "success", "data": {
        "gw_min_fresh_tokens": int(gw_db.get_setting("gw_min_fresh_tokens", "12")),
        "gw_refresh_batch": int(gw_db.get_setting("gw_refresh_batch", "6")),
        "gw_max_upstream_concurrency": int(gw_db.get_setting("gw_max_upstream_concurrency", "32")),
        "cb_failure_threshold": int(gw_db.get_setting("cb_failure_threshold", "5")),
        "cb_window_seconds": int(float(gw_db.get_setting("cb_window_seconds", "10"))),
        "cb_recovery_timeout": int(float(gw_db.get_setting("cb_recovery_timeout", "45"))),
        "short_cool_seconds": int(float(gw_db.get_setting("short_cool_seconds", "45"))),
        "escalate_fail_threshold": int(gw_db.get_setting("escalate_fail_threshold", "5")),
        "gw_renew_threshold_seconds": int(float(gw_db.get_setting("gw_renew_threshold_seconds", "300"))),
        "gw_renew_batch": int(gw_db.get_setting("gw_renew_batch", "10")),
        "quota_retire_threshold": int(float(gw_db.get_setting("quota_retire_threshold", "0"))),
        "gw_warm_concurrency": int(float(gw_db.get_setting("gw_warm_concurrency", "5"))),
    }}


@router.post("/api/gw-params")
async def save_gw_params(request: Request, user: str = Depends(authenticate_admin)):
    body = await request.json()
    _HI = 1_000_000_000  # 放开上限(安全大值)；仅保留正整数基础防错
    def _clamp(v, lo, hi, dft):
        try:
            v = int(v)
        except Exception:
            return dft
        return max(lo, min(hi, v))
    def _get(key, dft):
        """缺失字段回读当前DB值, 绝不用硬编码默认覆盖用户已保存配置"""
        if key in body:
            return body.get(key)
        return gw_db.get_setting(key, str(dft))
    mf = _clamp(_get("gw_min_fresh_tokens", 12), 1, _HI, 12)
    rb = _clamp(_get("gw_refresh_batch", 6), 1, _HI, 6)
    mc = _clamp(_get("gw_max_upstream_concurrency", 32), 1, _HI, 32)
    if rb > mf:
        rb = mf
    cft = _clamp(_get("cb_failure_threshold", 5), 1, _HI, 5)
    cws = _clamp(_get("cb_window_seconds", 10), 1, _HI, 10)
    crt = _clamp(_get("cb_recovery_timeout", 45), 0, _HI, 45)
    scs = _clamp(_get("short_cool_seconds", 45), 0, _HI, 45)
    eft = _clamp(_get("escalate_fail_threshold", 5), 0, _HI, 5)
    rth = _clamp(_get("gw_renew_threshold_seconds", 300), 60, _HI, 300)
    rba = _clamp(_get("gw_renew_batch", 10), 1, _HI, 10)
    gw_db.set_setting("gw_min_fresh_tokens", str(mf))
    gw_db.set_setting("gw_refresh_batch", str(rb))
    gw_db.set_setting("gw_max_upstream_concurrency", str(mc))
    gw_db.set_setting("cb_failure_threshold", str(cft))
    gw_db.set_setting("cb_window_seconds", str(cws))
    gw_db.set_setting("cb_recovery_timeout", str(crt))
    gw_db.set_setting("short_cool_seconds", str(scs))
    gw_db.set_setting("escalate_fail_threshold", str(eft))
    gw_db.set_setting("gw_renew_threshold_seconds", str(rth))
    gw_db.set_setting("gw_renew_batch", str(rba))
    qrt = _clamp(_get("quota_retire_threshold", 0), 0, _HI, 0)
    gw_db.set_setting("quota_retire_threshold", str(qrt))
    wcc = _clamp(_get("gw_warm_concurrency", 5), 1, 64, 5)
    gw_db.set_setting("gw_warm_concurrency", str(wcc))
    # 熔断器实时生效
    gw_pool.circuit.configure(failure_threshold=cft, window_seconds=float(cws), recovery_timeout=float(crt))
    add_log(f"[Gateway] 网关参数已更新: 水位={mf} 批次={rb} 并发={mc} | 熔断: {cft}次/{cws}s窗口/{crt}s恢复")
    return {"status": "success", "message": f"已保存: 水位{mf}/批次{rb}/并发{mc} | 熔断{cft}次/{cws}s/{crt}s | 临时冷却{scs}s/连败升级阈值{eft}次 (实时生效)", "data": {
        "gw_min_fresh_tokens": mf, "gw_refresh_batch": rb, "gw_max_upstream_concurrency": mc,
        "cb_failure_threshold": cft, "cb_window_seconds": cws, "cb_recovery_timeout": crt,
        "short_cool_seconds": scs, "escalate_fail_threshold": eft,
        "gw_renew_threshold_seconds": rth, "gw_renew_batch": rba, "quota_retire_threshold": qrt, "gw_warm_concurrency": wcc}}

@router.get("/api/co-params")
async def get_co_params(user: str = Depends(authenticate_admin)):
    return {"status": "success", "data": {
        "co_tier1_min_seconds": int(float(gw_db.get_setting("co_tier1_min_seconds", "480"))),
        "co_tier2_min_seconds": int(float(gw_db.get_setting("co_tier2_min_seconds", "180"))),
        "co_fresh_margin_seconds": int(float(gw_db.get_setting("co_fresh_margin_seconds", "30"))),
        "co_wait_ms": int(float(gw_db.get_setting("co_wait_ms", "300"))),
    }}


@router.post("/api/co-params")
async def save_co_params(request: Request, user: str = Depends(authenticate_admin)):
    body = await request.json()
    def _c(key, dft, lo, hi):
        try:
            v = int(float(body.get(key)))
        except Exception:
            v = int(float(gw_db.get_setting(key, str(dft))))
        return max(lo, min(hi, v))
    t1 = _c("co_tier1_min_seconds", 480, 30, 86400)
    t2 = _c("co_tier2_min_seconds", 180, 5, 86400)
    fm = _c("co_fresh_margin_seconds", 30, 1, 86400)
    wm = _c("co_wait_ms", 300, 0, 60000)
    if not (t1 > t2 > fm):
        t2 = max(t2, fm + 10)
        t1 = max(t1, t2 + 60)
    gw_db.set_setting("co_tier1_min_seconds", str(t1))
    gw_db.set_setting("co_tier2_min_seconds", str(t2))
    gw_db.set_setting("co_fresh_margin_seconds", str(fm))
    gw_db.set_setting("co_wait_ms", str(wm))
    add_log("[Selector] 选号调度参数已更新: 分档 %s/%s/%ss 冷路径等待 %sms (实时生效)" % (t1, t2, fm, wm))
    return {"status": "success", "message": "已保存: 档位 %s/%s/%ss | 冷路径等待 %sms (实时生效)" % (t1, t2, fm, wm), "data": {
        "co_tier1_min_seconds": t1, "co_tier2_min_seconds": t2,
        "co_fresh_margin_seconds": fm, "co_wait_ms": wm}}


@router.get("/api/login-enabled")
async def get_login_enabled(user: str = Depends(authenticate_admin)):
    return {"status": "success", "data": {"login_enabled": str(gw_db.get_setting("login_enabled", "true")).strip().lower() not in ("false", "0", "off", "no")}}


@router.post("/api/login-enabled")
async def set_login_enabled(request: Request, user: str = Depends(authenticate_admin)):
    body = await request.json()
    en = bool(body.get("login_enabled", True))
    gw_db.set_setting("login_enabled", "true" if en else "false")
    add_log("[Login] 登录系统已%s (实时生效)" % ("开启" if en else "关闭"))
    return {"status": "success", "message": "登录系统已%s" % ("开启" if en else "关闭"), "data": {"login_enabled": en}}

