import csv
import json
import logging
import os
import time
from typing import Dict, List, Optional
import httpx

logger = logging.getLogger("omnibot2api.pool")

class TokenItem:
    def __init__(
        self,
        email: str,
        access_token: str,
        refresh_token: str = "",
        user_id: str = "",
        active: bool = True,
    ):
        self.email = email
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.user_id = user_id
        self.active = active
        self.fail_count = 0
        self.last_used = 0.0

class TokenPool:
    def __init__(self, token_file: str, account_url: str):
        self.token_file = token_file
        self.account_url = account_url.rstrip("/")
        self.tokens: List[TokenItem] = []
        self._current_index = 0
        self.load_tokens()

    def load_tokens(self):
        if not os.path.exists(self.token_file):
            logger.warning(f"Token 文件 {self.token_file} 不存在，当前账号池为空")
            return

        loaded = []
        try:
            with open(self.token_file, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read().strip()
                if not content:
                    return

                # 判断是 CSV 还是纯文本
                lines = [l.strip() for l in content.splitlines() if l.strip() and not l.strip().startswith("#")]
                if not lines:
                    return

                # 检查第一行是否是 header
                first_line = lines[0].lower()
                is_csv = "," in first_line

                if is_csv:
                    reader = csv.DictReader(lines)
                    for row in reader:
                        email = row.get("邮箱", "").strip() or row.get("email", "").strip()
                        acc = row.get("accessToken", "").strip() or row.get("access_token", "").strip()
                        ref = row.get("refreshToken", "").strip() or row.get("refresh_token", "").strip()
                        uid = row.get("用户ID", "").strip() or row.get("userId", "").strip()
                        pwd = row.get("密码", "").strip() or row.get("password", "").strip()
                        status = row.get("校验状态", "").strip() or row.get("status", "").strip()
                        
                        # 过滤掉已明确标记为失效的账号
                        if acc and status != "已失效":
                            loaded.append(TokenItem(email=email, access_token=acc, refresh_token=ref, user_id=uid))
                else:
                    for line in lines:
                        parts = line.split()
                        if parts:
                            acc = parts[0].strip()
                            email = parts[1].strip() if len(parts) > 1 else ""
                            ref = parts[2].strip() if len(parts) > 2 else ""
                            loaded.append(TokenItem(email=email, access_token=acc, refresh_token=ref))

            self.tokens = loaded
            logger.info(f"成功载入 {len(self.tokens)} 个 OmniBot 账号 Token")
        except Exception as e:
            logger.error(f"读取 Token 文件失败: {e}")

    def get_active_token(self) -> Optional[TokenItem]:
        active_list = [t for t in self.tokens if t.active]
        if not active_list:
            return None

        # 简单轮询 Round-Robin
        self._current_index = (self._current_index + 1) % len(active_list)
        item = active_list[self._current_index]
        item.last_used = time.time()
        return item

    def mark_failed(self, item: TokenItem, status_code: int = 401):
        item.fail_count += 1
        logger.warning(f"账号 {item.email} 请求失败 (状态码: {status_code})，连续失败: {item.fail_count}")
        if status_code in (401, 403) or item.fail_count >= 3:
            if item.refresh_token:
                logger.info(f"正在尝试使用 refreshToken 刷新账号 {item.email}...")
                success = self.refresh_token(item)
                if success:
                    item.fail_count = 0
                    return
            item.active = False
            logger.error(f"账号 {item.email} Token 失效且无法自动刷新，已从活跃池下线")

    def refresh_token(self, item: TokenItem) -> bool:
        if not item.refresh_token:
            return False
        try:
            url = f"{self.account_url}/v1/auth/refresh"
            payload = {"refreshToken": item.refresh_token}
            with httpx.Client(timeout=15.0) as client:
                res = client.post(url, json=payload)
                if res.status_code == 200:
                    data = res.json()
                    new_acc = data.get("accessToken") or data.get("data", {}).get("accessToken")
                    new_ref = data.get("refreshToken") or data.get("data", {}).get("refreshToken")
                    if new_acc:
                        item.access_token = new_acc
                        if new_ref:
                            item.refresh_token = new_ref
                        item.active = True
                        logger.info(f"账号 {item.email} Token 刷新成功！")
                        self.save_tokens()
                        return True
        except Exception as e:
            logger.error(f"刷新账号 {item.email} 失败: {e}")
        return False

    def save_tokens(self):
        try:
            if not self.token_file.endswith(".csv"):
                return
            with open(self.token_file, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["email", "accessToken", "refreshToken", "userId", "updatedAt"])
                for t in self.tokens:
                    writer.writerow([t.email, t.access_token, t.refresh_token, t.user_id, time.strftime("%Y-%m-%d %H:%M:%S")])
        except Exception as e:
            logger.error(f"保存 Token 列表失败: {e}")

    def get_stats(self) -> Dict:
        total = len(self.tokens)
        active = len([t for t in self.tokens if t.active])
        return {"total": total, "active": active, "inactive": total - active}
