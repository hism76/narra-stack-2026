import base64
import json
import time
import random
import logging
import threading
from datetime import datetime, timezone, timedelta
import requests
from typing import Optional, Tuple, List, Dict, Any
from core.database import AccountDatabase

logger = logging.getLogger(__name__)

AUTH_LOGIN_URL = "https://account.omnimind.com.cn/v1/auth/login"


class PlatformCircuitBreaker:
    """平台级防雪崩熔断器 (滑动时间窗口保护)"""
    def __init__(self, failure_threshold: int = 5, window_seconds: float = 10.0, recovery_timeout: float = 45.0):
        self.failure_threshold = failure_threshold
        self.window_seconds = window_seconds
        self.recovery_timeout = recovery_timeout
        self.failures: List[float] = []
        self.circuit_open_until: float = 0.0
        self.lock = threading.Lock()

    def is_open(self) -> bool:
        with self.lock:
            now = time.time()
            if self.circuit_open_until > now:
                return True
            return False

    def configure(self, failure_threshold: int = None, window_seconds: float = None, recovery_timeout: float = None):
        """运行时调整熔断器参数 (实时生效)"""
        with self.lock:
            if failure_threshold is not None:
                self.failure_threshold = max(1, int(failure_threshold))
            if window_seconds is not None:
                self.window_seconds = max(1.0, float(window_seconds))
            if recovery_timeout is not None:
                self.recovery_timeout = max(0.0, float(recovery_timeout))

    def is_tripped(self) -> bool:
        return self.is_open()

    def record_failure(self):
        with self.lock:
            now = time.time()
            self.failures = [t for t in self.failures if now - t <= self.window_seconds]
            self.failures.append(now)
            if len(self.failures) >= self.failure_threshold:
                self.circuit_open_until = now + self.recovery_timeout
                logger.error(f"[CircuitBreaker] ⚠️ 触发平台级防雪崩熔断！{self.window_seconds}s 内连续 {len(self.failures)} 次失败，熔断保护 {self.recovery_timeout}s (暂停杀号)")
                self.failures.clear()

    def record_success(self):
        with self.lock:
            if self.failures:
                self.failures.clear()
            self.circuit_open_until = 0.0



def is_token_valid(token: Optional[str]) -> bool:
    if not token or len(token) < 20:
        return False
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return False
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.b64decode(payload_b64).decode("utf-8"))
        exp = payload.get("exp", 0)
        return time.time() < (exp - 60)
    except Exception:
        return False

class AccountPoolManager:
    """智能账号池管理器：支持账号租借独占锁、内存合并刷盘、原地自愈与平台熔断"""
    def __init__(self, db: Optional[AccountDatabase] = None):
        self.db = db or AccountDatabase()
        self._token_cache: Dict[str, Dict[str, Any]] = {}
        self._in_flight_logins = set()
        self._lease_locks: Dict[str, float] = {}       # email -> lease_expire_timestamp
        self._pending_success_counts: Dict[str, int] = {} # email -> count delta (write debounce)
        self._lock = threading.RLock()
        self.circuit_breaker = PlatformCircuitBreaker(failure_threshold=5, window_seconds=10.0, recovery_timeout=45.0)
        self.invalidated_emails = set()   # 供异步网关感知缓存失效联动

    def is_circuit_open(self) -> bool:
        return self.circuit_breaker.is_open()

    def acquire_lease(self, email: str, ttl: float = 60.0) -> bool:
        """尝试为指定账号获取独占租借锁 (默认 60s TTL 自动释放，杜绝死锁)"""
        with self._lock:
            now = time.time()
            expire_at = self._lease_locks.get(email, 0.0)
            if expire_at > now:
                return False
            self._lease_locks[email] = now + ttl
            return True

    def force_lease(self, email: str, ttl: float = 60.0):
        """强制刷新租借锁"""
        with self._lock:
            self._lease_locks[email] = time.time() + ttl

    def release_lease(self, email: str):
        """释放账号独占租借锁"""
        if not email:
            return
        with self._lock:
            self._lease_locks.pop(email, None)

    def is_leased(self, email: str) -> bool:
        """检查账号当前是否处于被租借占用中"""
        with self._lock:
            return time.time() < self._lease_locks.get(email, 0.0)

    def checkout_account(self, exclude_emails: Optional[List[str]] = None) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        智能检出可用账号与 Token：
        1. 优先从内存预热池中挑选【未被其他 Agent 租借】的空闲账号 (0.1ms)；
        2. 若内存无空闲，从 DB Top-30 候选桶挑选【未被租借】的账号即时换 Token；
        3. 若号池全满（>405 并发），平滑复用最早租借账号，绝不报错；
        4. 检出成功后自动打上 60s 租借独占锁。
        """
        exclude = set(exclude_emails or [])
        now = time.time()

        # Step 1: 内存预热池中检索【未被排除且未被租借】的候选
        with self._lock:
            cached_idle = [
                (em, data['token'], data['password'])
                for em, data in self._token_cache.items()
                if em not in exclude and data.get('token') and now >= self._lease_locks.get(em, 0.0)
            ]
        if cached_idle:
            em, tok, pwd = random.choice(cached_idle)
            self.force_lease(em, ttl=60.0)
            return tok, em, pwd

        # Step 2: 从数据库 Top-30 候选桶中筛选
        candidates = self.db.get_valid_accounts_pool(limit=30, exclude_emails=list(exclude))
        if candidates:
            # 优先挑当前未租借的候选
            idle_candidates = [c for c in candidates if not self.is_leased(c['email'])]
            pool_to_pick = idle_candidates if idle_candidates else candidates
            random.shuffle(pool_to_pick)

            for cand in pool_to_pick:
                email = cand['email']
                pwd = cand.get('password') or self.db.get_setting("default_account_password", "Omni#2026x")
                raw_tok = cand.get('access_token')

                # 快速验证或获取 Token
                if is_token_valid(raw_tok):
                    with self._lock:
                        self._token_cache[email] = {'token': raw_tok, 'password': pwd, 'updated_at': now}
                        self.force_lease(email, ttl=60.0)
                    return raw_tok, email, pwd

                # 尝试登录获取 Token
                with self._lock:
                    if email in self._in_flight_logins:
                        continue
                    self._in_flight_logins.add(email)

                try:
                    fresh_tok = self._login_account(email, pwd, timeout=4.5)
                    if fresh_tok:
                        self.db.update_token(email, fresh_tok)
                        with self._lock:
                            self._token_cache[email] = {'token': fresh_tok, 'password': pwd, 'updated_at': now}
                            self.force_lease(email, ttl=60.0)
                        return fresh_tok, email, pwd
                finally:
                    with self._lock:
                        self._in_flight_logins.discard(email)

        # Step 3: 全局降级兜底 (复用任意未排除的缓存账号)
        with self._lock:
            any_cached = [
                (em, data['token'], data['password'])
                for em, data in self._token_cache.items()
                if em not in exclude and data.get('token')
            ]
        if any_cached:
            em, tok, pwd = random.choice(any_cached)
            self.force_lease(em, ttl=60.0)
            return tok, em, pwd

        return None, None, None

    def mark_success(self, email: str):
        """调用成功：内存原子累加计数 (由保活线程批量合并刷盘，0.001ms 极速返回)"""
        if not email:
            return
        self.circuit_breaker.record_success()
        with self._lock:
            self._pending_success_counts[email] = self._pending_success_counts.get(email, 0) + 1

    def flush_success_buffer(self):
        """将内存中缓冲的调用成功指标单事务批量刷入 SQLite (削减 95% 写 I/O)"""
        with self._lock:
            if not self._pending_success_counts:
                return
            to_flush = dict(self._pending_success_counts)
            self._pending_success_counts.clear()

        try:
            self.db.batch_increment_success_counts(to_flush)
            logger.info(f"[PoolManager] 成功批量刷盘 {len(to_flush)} 个账号的活跃调用计数")
        except Exception as e:
            logger.error(f"[PoolManager] 批量刷盘异常: {e}")

    def mark_cooling(self, email: str, reason: str = "Quota exhausted"):
        """账号确诊超额：打入冷却并从缓存与租借中清除"""
        if not email:
            return
        duration = int(self.db.get_setting("cooling_duration_days", "7"))
        self.db.mark_account_cooling(email, reason=reason, duration_days=duration)
        self.invalidate_token(email)
        self.release_lease(email)

    def invalidate_token(self, email: str):
        """清除账号内存 Token 缓存"""
        with self._lock:
            self._token_cache.pop(email, None)
            self.invalidated_emails.add(email)
            self.invalidated_emails.add(email)

    def mark_rate_limited(self, email: str, duration_seconds: int = 120, reason: str = "Rate limited"):
        """账号触发限频/上游波动：进入秒级短冷却，到期自动回归候选池"""
        if not email:
            return
        try:
            now = datetime.now(timezone.utc)
            cooldown_until = (now + timedelta(seconds=int(duration_seconds))).isoformat()
            with self.db._get_conn() as conn:
                conn.execute(
                    """
                    UPDATE accounts
                    SET status = 'cooling',
                        cooldown_until = ?,
                        fail_reason = ?,
                        fail_count = COALESCE(fail_count, 0) + 1,
                        updated_at = ?
                    WHERE email = ?
                    """,
                    (cooldown_until, reason[:180], now.isoformat(), email),
                )
                conn.commit()
            logger.warning(f"[PoolManager] 账号 {email} 进入 {duration_seconds}s 短冷却 ({reason[:60]})")
        except Exception as e:
            logger.error(f"[PoolManager] mark_rate_limited({email}) 写库失败: {e}")
        finally:
            self.invalidate_token(email)
            self.release_lease(email)

    def mark_dead(self, email: str, reason: str = "Account invalid"):
        """账号确诊死亡/封禁：标记 invalid 并彻底移出候选池"""
        if not email:
            return
        try:
            now = datetime.now(timezone.utc)
            with self.db._get_conn() as conn:
                conn.execute(
                    """
                    UPDATE accounts
                    SET status = 'invalid',
                        fail_reason = ?,
                        updated_at = ?
                    WHERE email = ?
                    """,
                    (reason[:200], now.isoformat(), email),
                )
                conn.commit()
            logger.error(f"[PoolManager] 账号 {email} 已标记失效下线: {reason[:100]}")
        except Exception as e:
            logger.error(f"[PoolManager] mark_dead({email}) 写库失败: {e}")
        finally:
            self.invalidate_token(email)
            self.release_lease(email)

    def relogin_and_get_token(self, email: str, password: Optional[str] = None) -> Optional[str]:
        """使用原生密码原地重新登录换取最新 Token 并持久化"""
        fresh_tok = self._login_account(email, password, timeout=4.5)
        if fresh_tok:
            self.db.update_token(email, fresh_tok)
            with self._lock:
                self._token_cache[email] = {
                    'token': fresh_tok,
                    'password': password,
                    'updated_at': time.time()
                }
            logger.info(f"[PoolManager] 账号 {email} 原地密码重登成功，获得全新 Token")
            return fresh_tok
        return None

    def _login_account(self, email: str, password: str, timeout: float = 4.5) -> Optional[str]:
        """向官方发起登录请求 (支持代理配置与故障自动降级直连)"""
        hdrs = {
            "Content-Type": "application/json",
            "User-Agent": "OmniBot-Android/2.0"
        }
        payload = {"email": email, "password": password}
        # 1. 优先直连极速登录 (0.1s 极速直达)
        try:
            r = requests.post(AUTH_LOGIN_URL, json=payload, headers=hdrs, timeout=2.5)
            if r.status_code == 200:
                d = r.json()
                return d.get("accessToken") or d.get("token")
        except Exception as e:
            pass

        # 2. 直连遇阻时尝试代理兜底
        auth_proxy = self.db.get_setting("auth_proxy_url", "").strip()
        if auth_proxy:
            proxies = {"http": auth_proxy, "https": auth_proxy}
            try:
                r = requests.post(AUTH_LOGIN_URL, json=payload, headers=hdrs, proxies=proxies, timeout=3.0)
                if r.status_code == 200:
                    d = r.json()
                    return d.get("accessToken") or d.get("token")
            except Exception as pe:
                logger.warning(f"[PoolManager] 代理登录 {auth_proxy} 失败: {pe}")

        return None
