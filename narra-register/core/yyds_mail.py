import os
import time
import re
import requests
from typing import Optional, Dict, Any, List

class YYDSMailClient:
    BASE_URL = "https://maliapi.215.im/v1"
    DEFAULT_KEY = os.environ.get("YYDS_KEY", "") or os.environ.get("YYDS_MAIL_API_KEY", "")
    
    def __init__(self, api_key: str = ""):
        self.api_key = (api_key.strip() if api_key else "") or self.DEFAULT_KEY
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Content-Type": "application/json"
        })
        if self.api_key:
            self.session.headers["X-API-Key"] = self.api_key

    def _get_headers(self, temp_token: Optional[str] = None) -> Dict[str, str]:
        headers = {}
        if temp_token:
            headers["Authorization"] = f"Bearer {temp_token}"
        elif self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    def create_account(
        self,
        domain: Optional[str] = None,
        local_part: Optional[str] = None,
        subdomain: Optional[str] = None,
        platform_code: Optional[str] = None,
        exclude_domains: Optional[List[str]] = None,
        is_wildcard: bool = False
    ) -> Dict[str, Any]:
        """
        创建临时邮箱，支持固定域名、泛域名与平台选域
        """
        if is_wildcard:
            url = f"{self.BASE_URL}/accounts/wildcard"
        else:
            url = f"{self.BASE_URL}/accounts"

        payload: Dict[str, Any] = {}
        if local_part:
            payload["localPart"] = local_part
        if domain:
            payload["domain"] = domain
        if subdomain:
            payload["subdomain"] = subdomain
        if platform_code:
            payload["platformCode"] = platform_code
        if exclude_domains:
            payload["excludeDomains"] = exclude_domains

        resp = self.session.post(url, json=payload, headers=self._get_headers(), timeout=15)
        if resp.status_code not in (200, 201):
            raise Exception(f"YYDS Mail API request error ({resp.status_code}): {resp.text}")
        
        data = resp.json()
        if not data.get("success"):
            err_msg = data.get("error", "Unknown error")
            raise Exception(f"YYDS Mail create failed: {err_msg}")
        return data.get("data", {})

    def wait_for_verification_code(
        self,
        address: str,
        temp_token: Optional[str] = None,
        timeout: int = 60,
        stop_checker = None
    ) -> Optional[str]:
        """
        轮询等待最新验证码邮件 (精准提取 6 位大写/数字验证码，过滤普通英文单词)
        """
        start_time = time.time()
        common_words = {"PLEASE", "VERIFY", "THANKS", "REGIST", "SYSTEM", "NETMIN", "ONLINE", "SERVER", "NOTICE", "SIGNUP"}

        while time.time() - start_time < timeout:
            if stop_checker and stop_checker():
                return None
            try:
                url = f"{self.BASE_URL}/messages/next"
                params = {"address": address, "wait": 5}
                headers = self._get_headers(temp_token)
                resp = self.session.get(url, params=params, headers=headers, timeout=10)
                if resp.status_code == 200:
                    res_data = resp.json()
                    if res_data.get("success") and res_data.get("data"):
                        msg_info = res_data["data"]
                        msg = msg_info.get("message", {})
                        
                        # 1. 优先平台直接解析出的字段
                        code = msg.get("verificationCode")
                        if code and len(str(code).strip()) == 6:
                            return str(code).strip().upper()
                        
                        # 2. 文本关键词定位
                        text = msg.get("text", "") or ""
                        kw_match = re.search(r"(?:code|verification|验证码|code is|is)[:\s]+([A-Za-z0-9]{6})\b", text, re.I)
                        if kw_match:
                            cand = kw_match.group(1).upper()
                            if cand not in common_words:
                                return cand

                        # 3. 候选词过滤 (包含数字和大写字母)
                        candidates = re.findall(r"\b([A-Za-z0-9]{6})\b", text)
                        for c in candidates:
                            u = c.upper()
                            if u not in common_words and (any(ch.isdigit() for ch in u) or u.isupper()):
                                return u

                elif resp.status_code == 204:
                    pass
            except Exception:
                pass
            time.sleep(1)
        return None