from contextlib import contextmanager
import csv
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("database")

class AccountDatabase:
    """账号数据库管理类 (SQLite + WAL 模式 + 自动冷却与复活管理 + 桶内随机负载均衡)"""
    _db_initialized: set = set()   # 已建表的 db_path 集合 (DEF-002: 原类级布尔导致多实例异库跳建表)
    _settings_cache: Dict[str, Dict[str, str]] = {}   # db_path -> {key: value} 进程级缓存

    
    def record_account_success(self, email: str):
        try:
            self.batch_increment_success_counts({email: 1})
        except Exception:
            pass

    def record_account_failure(self, email: str, fail_reason: str = "Inference error"):
        try:
            self.mark_account_rate_limited(email, duration_seconds=60, reason=fail_reason)
        except Exception:
            pass
    def __init__(self, db_path: Optional[str] = None, csv_path: Optional[str] = None):
        if db_path:
            self.db_path = db_path
            data_dir = os.path.dirname(db_path)
        else:
            data_dir = os.environ.get("DATA_DIR", "/app/data")
            if not os.path.exists(data_dir) and os.path.exists("/home/developer/omnibot-stack/shared-data"):
                data_dir = "/home/developer/omnibot-stack/shared-data"
            self.db_path = os.path.join(data_dir, "narra.db")
        if data_dir and not os.path.exists(data_dir):
            try:
                os.makedirs(data_dir, exist_ok=True)
            except Exception:
                pass

        self.csv_path = csv_path or os.path.join(data_dir, "accounts.csv")
        if self.db_path not in AccountDatabase._db_initialized:
            try:
                self._init_db()
                AccountDatabase._db_initialized.add(self.db_path)
            except Exception:
                pass

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        # WAL mode is set permanently in _init_db
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        """初始化数据库表结构、索引与默认配置项"""
        with self._get_conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    email TEXT PRIMARY KEY,
                    password TEXT,
                    user_id TEXT,
                    access_token TEXT,
                    refresh_token TEXT,
                    access_expires_at TEXT,
                    refresh_expires_at TEXT,
                    status TEXT DEFAULT 'active',
                    cooldown_until TEXT,
                    fail_reason TEXT,
                    success_count INTEGER DEFAULT 0,
                    fail_count INTEGER DEFAULT 0,
                    created_at TEXT,
                    updated_at TEXT,
                    notes TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )
            
            # 建立高性能复合索引，支撑 Top-30 快速排序检索
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_accounts_pool_order 
                ON accounts (status, fail_count, success_count, updated_at);
                """
            )

            cursor = conn.execute("PRAGMA table_info(accounts)")
            existing_cols = [c["name"] for c in cursor.fetchall()]
            
            if "cooldown_until" not in existing_cols:
                conn.execute("ALTER TABLE accounts ADD COLUMN cooldown_until TEXT")
            if "fail_reason" not in existing_cols:
                conn.execute("ALTER TABLE accounts ADD COLUMN fail_reason TEXT")
            if "success_count" not in existing_cols:
                conn.execute("ALTER TABLE accounts ADD COLUMN success_count INTEGER DEFAULT 0")
            if "fail_count" not in existing_cols:
                conn.execute("ALTER TABLE accounts ADD COLUMN fail_count INTEGER DEFAULT 0")
            if "password" not in existing_cols:
                conn.execute("ALTER TABLE accounts ADD COLUMN password TEXT")
            if "balance_quota" not in existing_cols:
                conn.execute("ALTER TABLE accounts ADD COLUMN balance_quota INTEGER DEFAULT -1")
            if "quota_checked_at" not in existing_cols:
                conn.execute("ALTER TABLE accounts ADD COLUMN quota_checked_at TEXT")
            
            # 默认配置项初始化
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('cooling_duration_days', '7')")
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('gateway_timeout_seconds', '180')")
            conn.commit()

    def add_account(
        self,
        email: str,
        token: str,
        password: str = None,
        user_id: str = "",
        remark: str = "",
        status: str = "active",
        refresh_token: str = "",
    ) -> str:
        if password is None:
            password = self.get_setting("default_account_password", "Omni#2026x")
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO accounts
                (email, password, user_id, access_token, refresh_token, status, created_at, updated_at, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (email, password, user_id, token, refresh_token, status, now, now, remark),
            )
            conn.commit()
        return email

    def save_account(self, data: Dict[str, Any]):
        email = data.get("email")
        if not email:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO accounts
                (email, password, user_id, access_token, refresh_token, access_expires_at, refresh_expires_at, status, cooldown_until, fail_reason, created_at, updated_at, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    email,
                    data.get("password", self.get_setting("default_account_password", "Omni#2026x")),
                    data.get("user_id", ""),
                    data.get("access_token", ""),
                    data.get("refresh_token", ""),
                    data.get("access_expires_at", ""),
                    data.get("refresh_expires_at", ""),
                    data.get("status", "active"),
                    data.get("cooldown_until", ""),
                    data.get("fail_reason", ""),
                    data.get("created_at", now),
                    now,
                    data.get("notes", ""),
                ),
            )
            conn.commit()

    def mark_account_rate_limited(self, email: str, duration_seconds: int = 120, reason: str = "Rate limited"):
        """将账号标记为秒级短冷却"""
        now = datetime.now(timezone.utc)
        cooldown_until = (now + timedelta(seconds=int(duration_seconds))).isoformat()
        with self._get_conn() as conn:
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
        logger.info(f"[DB] 账号 {email} 已置入短冷却 ({duration_seconds}s)，截至时间: {cooldown_until}")

    def mark_account_dead(self, email: str, reason: str = "Account invalid"):
        """账号确诊死亡/封禁：标记 invalid 并彻底移出候选池"""
        now = datetime.now(timezone.utc)
        with self._get_conn() as conn:
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
        logger.warning(f"[DB] 账号 {email} 已标记失效下线: {reason[:100]}")

    def mark_account_cooling(self, email: str, duration_days: float = 7.0, reason: str = "Quota exhausted"):
        """将账号标记为冷却状态，计算冷却截至时间戳"""
        now = datetime.now(timezone.utc)
        cooldown_until = (now + timedelta(days=duration_days)).isoformat()
        with self._get_conn() as conn:
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
                (cooldown_until, reason, now.isoformat(), email),
            )
            conn.commit()
        logger.info(f"[DB] 账号 {email} 已置入冷却状态，截至时间: {cooldown_until}")

    def get_refresh_token(self, email: str) -> str:
        """读取账号保存的 refreshToken (无则返回空串)"""
        try:
            with self._get_conn() as conn:
                row = conn.execute("SELECT refresh_token FROM accounts WHERE email = ?", (email,)).fetchone()
            return (row["refresh_token"] or "") if row else ""
        except Exception:
            return ""

    def update_quota(self, email: str, balance_quota: int):
        """记录账号剩余额度 (new_api_quota 原始单位), -1 表示未知"""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE accounts SET balance_quota = ?, quota_checked_at = ?, updated_at = ? WHERE email = ?",
                (balance_quota, now, now, email)
            )
            conn.commit()

    def update_token(self, email: str, new_token: str, refresh_token: Optional[str] = None,
                     refresh_expires_at: Optional[str] = None):
        """更新单个账号的 access_token (可顺带保存 refreshToken 及其到期时间)"""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            if refresh_token is not None:
                conn.execute(
                    "UPDATE accounts SET access_token = ?, refresh_token = ?, refresh_expires_at = ?, updated_at = ? WHERE email = ?",
                    (new_token, refresh_token, refresh_expires_at or "", now, email)
                )
            else:
                conn.execute(
                    "UPDATE accounts SET access_token = ?, updated_at = ? WHERE email = ?",
                    (new_token, now, email)
                )
            conn.commit()

    def update_account_token(self, email: str, new_token: str):
        return self.update_token(email, new_token)

    def mark_account_active(self, email: str, new_token: Optional[str] = None):
        """成功调用后刷新活跃时间，清除冷却与失败计数"""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            if new_token:
                conn.execute(
                    """
                    UPDATE accounts
                    SET status = 'active',
                        access_token = ?,
                        cooldown_until = NULL,
                        fail_reason = NULL,
                        fail_count = 0,
                        success_count = COALESCE(success_count, 0) + 1,
                        updated_at = ?
                    WHERE email = ?
                    """,
                    (new_token, now, email),
                )
            else:
                conn.execute(
                    """
                    UPDATE accounts
                    SET status = 'active',
                        cooldown_until = NULL,
                        fail_reason = NULL,
                        fail_count = 0,
                        success_count = COALESCE(success_count, 0) + 1,
                        updated_at = ?
                    WHERE email = ?
                    """,
                    (now, email),
                )
            conn.commit()

    def get_valid_accounts_pool(self, limit: int = 30, exclude_emails: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """
        获取当前可用账号的候选池 (支持排除列表与多维最优负载均衡排序)
        排序规则：最少失败优先 -> 最少调用优先 -> 最久未活跃优先
        若全部处于冷却期，则自动兜底挑选离复活时间最近的前 5 个号做单次探活
        """
        now_str = datetime.now(timezone.utc).isoformat()
        exclude = exclude_emails or []
        conn = self._get_conn()
        sql = """
            SELECT rowid as id, email, password, access_token, refresh_token, user_id, notes, status, cooldown_until,
                   COALESCE(success_count, 0) as success_count,
                   COALESCE(fail_count, 0) as fail_count,
                   updated_at
            FROM accounts
            WHERE password IS NOT NULL
              AND (status = 'active' OR (status = 'cooling' AND cooldown_until IS NOT NULL AND cooldown_until <= ?))
        """
        params = [now_str]
        if exclude:
            placeholders = ','.join(['?'] * len(exclude))
            sql += f" AND email NOT IN ({placeholders})"
            params.extend(exclude)

        sql += """
            ORDER BY CASE WHEN COALESCE(success_count, 0) > 0 THEN 0 ELSE 1 END ASC,
                     COALESCE(fail_count, 0) ASC,
                     COALESCE(success_count, 0) DESC,
                     COALESCE(updated_at, '') ASC
            LIMIT ?
        """
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        if rows:
            return [dict(r) for r in rows]

        fallback_sql = """
            SELECT rowid as id, email, password, access_token, refresh_token, user_id, notes, status, cooldown_until,
                   COALESCE(success_count, 0) as success_count,
                   COALESCE(fail_count, 0) as fail_count,
                   updated_at
            FROM accounts
            WHERE password IS NOT NULL
        """
        f_params = []
        if exclude:
            placeholders = ','.join(['?'] * len(exclude))
            fallback_sql += f" AND email NOT IN ({placeholders})"
            f_params.extend(exclude)
        fallback_sql += " ORDER BY COALESCE(cooldown_until, '9999') ASC LIMIT 5"
        fb_rows = conn.execute(fallback_sql, f_params).fetchall()
        return [dict(r) for r in fb_rows]

    def revive_expired_cooling(self) -> int:
        """将冷却已到期的账号自动翻回 active (保持状态字段与功能一致)"""
        now_str = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            cur = conn.execute(
                """
                UPDATE accounts
                SET status = 'active',
                    updated_at = ?
                WHERE status = 'cooling' AND cooldown_until IS NOT NULL AND cooldown_until <= ?
                """,
                (now_str, now_str),
            )
            conn.commit()
            if cur.rowcount:
                logger.info(f"[DB] 自动复活 {cur.rowcount} 个冷却到期账号")
            return cur.rowcount

    def reset_all_cooling_accounts(self) -> int:
        """��键唤醒/重置所有处于冷却中的账号"""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            cursor = conn.execute(
                """
                UPDATE accounts
                SET status = 'active',
                    cooldown_until = NULL,
                    fail_reason = NULL,
                    updated_at = ?
                WHERE status = 'cooling'
                """,
                (now,),
            )
            conn.commit()
            count = cursor.rowcount
            logger.info(f"[DB] 已一键唤醒 {count} 个冷却账号")
            return count

    def get_pool_status_summary(self) -> Dict[str, int]:
        now_str = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
            active = conn.execute(
                "SELECT COUNT(*) FROM accounts WHERE status = 'active' OR (status = 'cooling' AND (cooldown_until IS NULL OR cooldown_until <= ?))",
                (now_str,)
            ).fetchone()[0]
            cooling = conn.execute(
                "SELECT COUNT(*) FROM accounts WHERE status = 'cooling' AND cooldown_until > ?",
                (now_str,)
            ).fetchone()[0]
            invalid = conn.execute(
                "SELECT COUNT(*) FROM accounts WHERE status NOT IN ('active', 'cooling')",
            ).fetchone()[0]
            return {
                "total_accounts": total,
                "active_accounts": active,
                "cooling_accounts": cooling,
                "invalid_accounts": invalid,
            }

    def search_accounts(
        self,
        page: int = 1,
        page_size: int = 50,
        search: str = "",
        status_filter: str = "all"
    ) -> Tuple[List[Dict[str, Any]], int]:
        """支持邮箱搜索与状态筛选的综合分页查询"""
        query_conditions = []
        params = []
        now_str = datetime.now(timezone.utc).isoformat()

        if search and search.strip():
            query_conditions.append("email LIKE ?")
            params.append(f"%{search.strip()}%")

        if status_filter and status_filter != "all":
            if status_filter == "active":
                query_conditions.append("(status = 'active' OR (status = 'cooling' AND (cooldown_until IS NULL OR cooldown_until <= ?)))")
                params.append(now_str)
            elif status_filter == "cooling":
                query_conditions.append("status = 'cooling' AND cooldown_until > ?")
                params.append(now_str)
            elif status_filter in ("invalid", "expired", "error"):
                query_conditions.append("status NOT IN ('active', 'cooling')")

        where_clause = ""
        if query_conditions:
            where_clause = "WHERE " + " AND ".join(query_conditions)

        offset = max(0, (page - 1) * page_size)

        with self._get_conn() as conn:
            count_sql = f"SELECT COUNT(*) FROM accounts {where_clause}"
            total = conn.execute(count_sql, params).fetchone()[0]

            select_sql = f"""
                SELECT rowid as id, email, password, status, cooldown_until, fail_reason, 
                       COALESCE(success_count, 0) as success_count, 
                       COALESCE(fail_count, 0) as fail_count, 
                       COALESCE(balance_quota, -1) as balance_quota,
                       created_at, updated_at 
                FROM accounts 
                {where_clause} 
                ORDER BY rowid DESC 
                LIMIT ? OFFSET ?
            """
            rows = conn.execute(select_sql, params + [page_size, offset]).fetchall()
            out = []
            for r in rows:
                item = dict(r)
                # 冷却分类: 依据剩余时长 heuristic(>=1h视为长期/天级, 否则临时)
                if item.get("status") == "cooling" and item.get("cooldown_until"):
                    try:
                        from datetime import datetime as _dt
                        cu = _dt.fromisoformat(str(item["cooldown_until"]))
                        # DEF-001(P1): 写入方是 aware 时间戳(+00:00), naive utcnow() 相减抛 TypeError 被吞
                        if cu.tzinfo is None:  # 历史 naive 数据按 UTC 语义补齐
                            cu = cu.replace(tzinfo=timezone.utc)
                        left = (cu - datetime.now(timezone.utc)).total_seconds()
                    except Exception:
                        left = 0
                    item["cooldown_left_seconds"] = max(0, int(left))
                    item["cooldown_type"] = "long" if left >= 3600 else "temp"
                else:
                    item["cooldown_left_seconds"] = 0
                    item["cooldown_type"] = ""
                out.append(item)
            return out, total

    def get_emails_by_identifiers(self, identifiers: List[Any]) -> List[str]:
        """根据 rowid 或 email 列表查询对应的所有 email 字符串"""
        if not identifiers:
            return []
        emails = []
        with self._get_conn() as conn:
            for ident in identifiers:
                if isinstance(ident, int) or (isinstance(ident, str) and ident.isdigit()):
                    row = conn.execute("SELECT email FROM accounts WHERE rowid = ?", (int(ident),)).fetchone()
                    if row:
                        emails.append(row["email"])
                else:
                    emails.append(str(ident))
        return emails

    def delete_account(self, identifier: Any) -> Tuple[bool, Optional[str]]:
        """删除账号（支持 rowid 或 email，返回是否成功与对应 email）"""
        with self._get_conn() as conn:
            email_to_del = None
            if isinstance(identifier, int) or (isinstance(identifier, str) and identifier.isdigit()):
                row = conn.execute("SELECT email FROM accounts WHERE rowid = ?", (int(identifier),)).fetchone()
                if row:
                    email_to_del = row["email"]
                cursor = conn.execute("DELETE FROM accounts WHERE rowid = ?", (int(identifier),))
            else:
                email_to_del = str(identifier)
                cursor = conn.execute("DELETE FROM accounts WHERE email = ?", (str(identifier),))
            conn.commit()
            return (cursor.rowcount > 0, email_to_del)

    def batch_delete_accounts(self, identifiers: List[Any]) -> Tuple[int, List[str]]:
        """批量删除账号（返回删除条数与被删 email 列表）"""
        if not identifiers:
            return (0, [])
        deleted = 0
        deleted_emails = []
        with self._get_conn() as conn:
            for i in range(0, len(identifiers), 100):
                chunk = identifiers[i : i + 100]
                emails = [str(x) for x in chunk if not str(x).isdigit()]
                ids = [int(x) for x in chunk if str(x).isdigit()]
                if emails:
                    placeholders = ",".join(["?"] * len(emails))
                    cur = conn.execute(f"DELETE FROM accounts WHERE email IN ({placeholders})", emails)
                    deleted += cur.rowcount
                    deleted_emails.extend(emails)
                if ids:
                    placeholders = ",".join(["?"] * len(ids))
                    rows = conn.execute(f"SELECT email FROM accounts WHERE rowid IN ({placeholders})", ids).fetchall()
                    for r in rows:
                        deleted_emails.append(r["email"])
                    cur = conn.execute(f"DELETE FROM accounts WHERE rowid IN ({placeholders})", ids)
                    deleted += cur.rowcount
            conn.commit()
        return (deleted, deleted_emails)

    def batch_reset_cooling(self, identifiers: List[Any]) -> int:
        """批量唤醒/重置选中的冷却账号"""
        if not identifiers:
            return 0
        reset_count = 0
        now_str = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            for i in range(0, len(identifiers), 100):
                chunk = identifiers[i : i + 100]
                emails = [str(x) for x in chunk if not str(x).isdigit()]
                ids = [int(x) for x in chunk if str(x).isdigit()]
                if emails:
                    placeholders = ",".join(["?"] * len(emails))
                    cur = conn.execute(
                        f"UPDATE accounts SET status = 'active', cooldown_until = NULL, fail_reason = NULL, updated_at = ? WHERE email IN ({placeholders})",
                        [now_str] + emails
                    )
                    reset_count += cur.rowcount
                if ids:
                    placeholders = ",".join(["?"] * len(ids))
                    cur = conn.execute(
                        f"UPDATE accounts SET status = 'active', cooldown_until = NULL, fail_reason = NULL, updated_at = ? WHERE rowid IN ({placeholders})",
                        [now_str] + ids
                    )
                    reset_count += cur.rowcount
            conn.commit()
        return reset_count

    def import_accounts(self, items: List[Dict[str, str]]) -> Dict[str, int]:
        """批量导入/增量更新账号"""
        now = datetime.now(timezone.utc).isoformat()
        added = 0
        updated = 0
        with self._get_conn() as conn:
            for item in items:
                email = item.get("email", "").strip()
                password = item.get("password", self.get_setting("default_account_password", "Omni#2026x")).strip()
                if not email or "@" not in email:
                    continue
                existing = conn.execute("SELECT email FROM accounts WHERE email = ?", (email,)).fetchone()
                if existing:
                    conn.execute("UPDATE accounts SET password = ?, updated_at = ? WHERE email = ?", (password, now, email))
                    updated += 1
                else:
                    conn.execute(
                        "INSERT INTO accounts (email, password, status, created_at, updated_at) VALUES (?, ?, 'active', ?, ?)",
                        (email, password, now, now)
                    )
                    added += 1
            conn.commit()
        return {"added": added, "updated": updated, "total": added + updated}

    def get_accounts_for_quota_refresh(self, limit: int = 5) -> List[Dict[str, Any]]:
        """NED-004: 取 quota_checked_at 最旧 (从未查过的最先) 的 active 账号用于额度轮转。
        quota_checked_at 为空/NULL 排最前 (COALESCE 到远古时间), 保证未查过的账号优先进一轮。"""
        with self._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT email, password, access_token, refresh_token, status
                FROM accounts
                WHERE status = 'active'
                ORDER BY COALESCE(quota_checked_at, '1970-01-01T00:00:00+00:00') ASC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_all_accounts_for_health_check(self) -> List[Dict[str, Any]]:
        """获取用于全池体检的全量账号列表"""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT rowid as id, email, password, access_token, refresh_token, user_id, notes, status, cooldown_until FROM accounts ORDER BY rowid ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def record_health_check_result(
        self,
        email: str,
        is_healthy: bool,
        status_code: int,
        orig_status: str,
        orig_cooldown_until: Optional[str] = None,
        new_token: Optional[str] = None,
        err_msg: str = ""
    ) -> str:
        """
        记录单个账号的体检结果，严格防范顺延 Bug：
        - 200: 满血复活/保持活跃，清除 cooldown_until
        - 503 且原状态是 cooling: 严格保留原 cooldown_until 剩余时间，绝不重新计时 7 天！
        - 503 且原状态是 active: 触发正常 7 天冷却
        - 400/403/重登失败: 标记为 invalid
        """
        now = datetime.now(timezone.utc)
        now_str = now.isoformat()
        result_action = "none"

        with self._get_conn() as conn:
            if is_healthy:
                token_update_sql = ", access_token = ?" if new_token else ""
                params = [now_str, email] if not new_token else [now_str, new_token, email]
                conn.execute(
                    f"""
                    UPDATE accounts 
                    SET status = 'active', 
                        cooldown_until = NULL, 
                        fail_reason = NULL,
                        success_count = COALESCE(success_count, 0) + 1,
                        updated_at = ? {token_update_sql}
                    WHERE email = ?
                    """,
                    params
                )
                result_action = "revived" if orig_status == "cooling" else "healthy"

            elif status_code == 503:
                if orig_status == "cooling":
                    # 原本已处于冷却中：严格保留原有冷却时间，绝不重置！
                    conn.execute(
                        """
                        UPDATE accounts 
                        SET updated_at = ?,
                            fail_reason = 'Health check: Still exhausted (503)'
                        WHERE email = ?
                        """,
                        (now_str, email)
                    )
                    result_action = "cooling_kept"
                else:
                    # 原本处于 active：初次发现 503，正常置入 7 天冷却
                    cooldown_until = (now + timedelta(days=7.0)).isoformat()
                    conn.execute(
                        """
                        UPDATE accounts 
                        SET status = 'cooling', 
                            cooldown_until = ?, 
                            fail_reason = 'Health check: Quota exhausted (503)',
                            fail_count = COALESCE(fail_count, 0) + 1,
                            updated_at = ?
                        WHERE email = ?
                        """,
                        (cooldown_until, now_str, email)
                    )
                    result_action = "cooling_new"

            elif status_code in (400, 401, 403):
                conn.execute(
                    """
                    UPDATE accounts 
                    SET status = 'invalid', 
                        fail_reason = ?,
                        updated_at = ?
                    WHERE email = ?
                    """,
                    (f"Health check login error ({status_code}): {err_msg[:80]}", now_str, email)
                )
                result_action = "invalid"
            else:
                # 500/超时(0)/其他意外码: 计失败一次但不改状态, 避免误杀
                conn.execute(
                    """
                    UPDATE accounts
                    SET fail_count = COALESCE(fail_count, 0) + 1,
                        fail_reason = ?,
                        updated_at = ?
                    WHERE email = ?
                    """,
                    (f"Health check transient ({status_code}): {err_msg[:80]}", now_str, email)
                )
                result_action = "transient"
            
            conn.commit()
        return result_action

    def get_all_accounts_for_export(self) -> List[Dict[str, Any]]:
        """获取全量账号用于 CSV 导出"""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT rowid as id, email, password, status, cooldown_until, fail_reason, success_count, fail_count, created_at, updated_at FROM accounts ORDER BY rowid DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def _cache_get(self, key: str):
        cache = AccountDatabase._settings_cache.get(self.db_path)
        if cache is None:
            return None
        return cache.get(key)

    def _cache_set(self, key: str, value: str):
        cache = AccountDatabase._settings_cache.setdefault(self.db_path, {})
        cache[key] = value

    def get_setting(self, key: str, default: str = "") -> str:
        # 先查进程级缓存, 命中即返回 (请求热路径消除同步SQLite建连)
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        with self._get_conn() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            val = row["value"] if row else default
        self._cache_set(key, val)
        return val

    def set_setting(self, key: str, value: str):
        with self._get_conn() as conn:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
            conn.commit()
        self._cache_set(key, value)

    def get_all_settings(self) -> Dict[str, str]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
            return {r["key"]: r["value"] for r in rows}

    def batch_update_tokens(self, updates: List[Tuple[str, str]]):
        """批量写入最新 Token 并更新活跃时间戳 (单事务)"""
        if not updates:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.executemany(
                """
                UPDATE accounts
                SET access_token = ?,
                    status = 'active',
                    cooldown_until = NULL,
                    fail_reason = NULL,
                    updated_at = ?
                WHERE email = ?
                """,
                [(tok, now, email) for email, tok in updates]
            )
            conn.commit()

    def batch_increment_success_counts(self, counts: Dict[str, int]):
        """批量增加账号调用成功计数并刷新活跃时间 (单事务极速合并写入)"""
        if not counts:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.executemany(
                """
                UPDATE accounts
                SET success_count = COALESCE(success_count, 0) + ?,
                    fail_count = 0,
                    updated_at = ?
                WHERE email = ?
                """,
                [(cnt, now, email) for email, cnt in counts.items()]
            )
            conn.commit()

    def get_top_healthy_candidates(self, limit: int = 30, exclude_emails: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """获取当前可用候选账号 (支持排除列表与多维最优负载均衡排序)"""
        now_str = datetime.now(timezone.utc).isoformat()
        exclude = exclude_emails or []
        with self._get_conn() as conn:
            sql = """
                SELECT rowid as id, email, password, access_token, refresh_token, user_id, notes, status, cooldown_until,
                       COALESCE(success_count, 0) as success_count,
                       COALESCE(fail_count, 0) as fail_count,
                       updated_at
                FROM accounts
                WHERE password IS NOT NULL
                  AND (status = 'active' OR (status = 'cooling' AND cooldown_until IS NOT NULL AND cooldown_until <= ?))
            """
            params = [now_str]
            if exclude:
                placeholders = ','.join(['?'] * len(exclude))
                sql += f" AND email NOT IN ({placeholders})"
                params.extend(exclude)

            sql += """
                ORDER BY COALESCE(fail_count, 0) ASC, 
                         COALESCE(success_count, 0) ASC, 
                         COALESCE(updated_at, '') ASC
                LIMIT ?
            """
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            if rows:
                return [dict(r) for r in rows]

            # 兜底查询
            fallback_sql = """
                SELECT rowid as id, email, password, access_token, refresh_token, user_id, notes, status, cooldown_until,
                       COALESCE(success_count, 0) as success_count,
                       COALESCE(fail_count, 0) as fail_count,
                       updated_at
                FROM accounts
                WHERE password IS NOT NULL
            """
            f_params = []
            if exclude:
                placeholders = ','.join(['?'] * len(exclude))
                fallback_sql += f" AND email NOT IN ({placeholders})"
                f_params.extend(exclude)
            fallback_sql += " ORDER BY cooldown_until ASC LIMIT ?"
            f_params.append(min(limit, 5))
            rows = conn.execute(fallback_sql, f_params).fetchall()
            return [dict(r) for r in rows]