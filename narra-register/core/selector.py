# -*- coding: utf-8 -*-
"""选号引擎 (与风控/熔断解耦): 寿命分档 + 档内严格轮询 + 冷路径限时等待"""
import asyncio
import time
from typing import Optional, Dict, List, Any, Tuple


class AccountSelector:
    """从 token 缓存按剩余寿命分三档选号：充裕档优先轮询，快过期档只做短活兜底"""

    def __init__(self, db):
        self.db = db
        self._rr = 0
        self._refill_cond = None

    def _p(self, key, dft, lo):
        try:
            v = float(self.db.get_setting(key, str(dft)))
        except Exception:
            v = dft
        return v if v >= lo else lo

    def tier1_min(self):
        return self._p('co_tier1_min_seconds', 480.0, 30.0)

    def tier2_min(self):
        return self._p('co_tier2_min_seconds', 180.0, 5.0)

    def fresh_margin(self):
        return self._p('co_fresh_margin_seconds', 30.0, 1.0)

    def wait_ms(self):
        return int(self._p('co_wait_ms', 300.0, 0.0))

    def notify_refill(self):
        """广播紧急补位(Condition), 唤醒维护循环立即补货。无竞态: 不用 set/clear"""
        try:
            cond = self._refill_cond
            if cond is not None:
                loop = None
                try:
                    loop = asyncio.get_running_loop()
                except Exception:
                    loop = None
                async def _n():
                    async with cond:
                        cond.notify_all()
                if loop is not None:
                    loop.create_task(_n())
                else:
                    # 兜底: 若无运行循环(罕见), 直接忽略, 10s例行巡检兜底
                    pass
        except Exception:
            pass

    def bind_refill_cond(self, cond):
        self._refill_cond = cond

    def pick(self, tokens, exclude=None, request_hint_long=True):
        excl = set(exclude or [])
        now = time.time()
        t1 = self.tier1_min()
        t2 = self.tier2_min()
        margin = self.fresh_margin()
        tiers = ([], [], [])
        for e, d in tokens.items():
            if e in excl or not d or not d.get('token'):
                continue
            left = d.get('exp', 0) - now
            if left <= margin:
                continue
            if left >= t1:
                tiers[0].append((e, d))
            elif left >= t2:
                tiers[1].append((e, d))
            else:
                tiers[2].append((e, d))
        usable = (tiers[0] or tiers[1]) if request_hint_long else (tiers[0] or tiers[1] or tiers[2])
        if not usable:
            return None
        self._rr += 1
        return usable[self._rr % len(usable)]
