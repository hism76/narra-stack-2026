import os
import re
import time
import random
import string
import logging
import requests
import httpx
import urllib3
from typing import Optional, Dict, Any, Tuple, List
from Crypto.Cipher import DES
from Crypto.Util.Padding import pad

urllib3.disable_warnings()
logger = logging.getLogger("narra_auth")

class NarraNexusClient:
    NARRA_BASE = "https://agent.narra.nexus"
    NETMIND_AUTH = "https://auth-api.netmind.ai"
    CLASH_API = "http://clash-proxy:9090"
    CLASH_SECRET = "clash123456"

    def __init__(
        self,
        narra_base: Optional[str] = None,
        netmind_auth: Optional[str] = None,
        proxy: Optional[str] = None
    ):
        self.narra_base = (narra_base or self.NARRA_BASE).rstrip("/")
        self.netmind_auth = (netmind_auth or self.NETMIND_AUTH).rstrip("/")
        self.proxy = proxy.strip() if (proxy and proxy.strip()) else os.environ.get("NETMIND_PROXY", "http://clash-proxy:7890")
        
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*"
        })
        self.session.verify = False

    def _get_httpx_kwargs(self) -> Dict[str, Any]:
        proxy_url = self.proxy or os.environ.get("NETMIND_PROXY")
        kwargs = {"verify": False, "timeout": 15.0}
        if proxy_url:
            kwargs["proxy"] = proxy_url
        return kwargs

    def rotate_clash_proxy(self) -> Optional[str]:
        """通过 Clash 核心 API 自动轮换至下一个优质海外节点，获取新出口 IP"""
        try:
            url = f"{self.CLASH_API}/proxies/%F0%9F%90%9F%E6%BC%8F%E7%BD%91%E4%B9%8B%E9%B1%BC"
            headers = {"Authorization": f"Bearer {self.CLASH_SECRET}"}
            with httpx.Client(timeout=4.0) as client:
                resp = client.get(url, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    all_nodes = [n for n in data.get("all", []) if n not in ("DIRECT", "REJECT", "🚀节点选择")]
                    cur = data.get("now")
                    if all_nodes:
                        idx = (all_nodes.index(cur) + 1) % len(all_nodes) if cur in all_nodes else 0
                        next_node = all_nodes[idx]
                        client.put(url, json={"name": next_node}, headers=headers)
                        logger.info(f"[ClashRotate] 智能切换代理节点: {cur} -> {next_node}")
                        return next_node
        except Exception as e:
            logger.warning(f"[ClashRotate] 节点切换非阻断提示: {e}")
        return None

    @staticmethod
    def generate_compliant_password() -> str:
        """生成符合 NetMind 策略的强密码 (8-16位, 包含大小写、数字及特殊字符)"""
        uppers = random.choice(string.ascii_uppercase)
        lowers = random.choice(string.ascii_lowercase)
        digits = random.choice(string.digits)
        specials = random.choice("!@#$%&*")
        rest = [random.choice(string.ascii_letters + string.digits) for _ in range(8)]
        all_chars = list(uppers + lowers + digits + specials + "".join(rest))
        random.shuffle(all_chars)
        return "".join(all_chars)

    @staticmethod
    def _des_cbc_encrypt(text: str, key_iv: str) -> str:
        key_bytes = key_iv.encode("utf-8")
        cipher = DES.new(key_bytes, DES.MODE_CBC, key_bytes)
        padded = pad(text.encode("utf-8"), DES.block_size, style="pkcs7")
        return cipher.encrypt(padded).hex()

    @staticmethod
    def _random_sign_str(n: int = 8) -> str:
        chars = string.ascii_letters + string.digits
        return "".join(random.choice(chars) for _ in range(n))

    def send_signup_code(self, email: str, max_retries: int = 3, log_callback = None) -> bool:
        """
        高可用双通道发码：
        1. 优先尝试 NarraNexus 前端发码
        2. 自动兜底 NetMind 原生接口
        3. 遇到频控、网络重置或 EOF 时自动切换 Clash 节点重试，杜绝单 IP 限制
        """
        email = email.strip().lower()

        for attempt in range(1, max_retries + 1):
            # 1. 尝试 Narra 前端通道
            try:
                url = f"{self.narra_base}/api/auth/signup/send-code"
                resp = self.session.post(url, json={"email": email}, timeout=8)
                if resp.status_code == 200 and resp.json().get("success"):
                    return True
            except Exception:
                pass

            # 2. 尝试 NetMind 官方原生接口通道 (走代理)
            last_err = ""
            try:
                with httpx.Client(**self._get_httpx_kwargs()) as client:
                    resp = client.post(
                        f"{self.netmind_auth}/register/sendCode",
                        data={"email": email, "type": 1},
                        headers={"Content-Type": "application/x-www-form-urlencoded"}
                    )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("success"):
                        return True
                    last_err = str(data.get("msg") or data.get("errorcode") or "")
                else:
                    last_err = f"HTTP {resp.status_code}"
            except Exception as e:
                last_err = str(e)

            # 判断是否触发频控或网络中断
            is_rate_limit = any(k in last_err.lower() for k in ["frequent", "too many times", "operation_too_often", "429", "eof", "ssl", "timeout"])
            if attempt < max_retries:
                # 自动轮换代理节点获得新出口 IP
                new_node = self.rotate_clash_proxy()
                msg = f"发码遭遇限制/抖动 ({last_err})，已自动轮换节点至 {new_node or '新线路'}，重试 ({attempt}/{max_retries})..."
                logger.info(f"[send_code] {msg}")
                if log_callback:
                    try: log_callback(msg)
                    except Exception: pass
                time.sleep(2.0)
            else:
                raise Exception(f"触发验证码发送失败 ({last_err})，已重试 {max_retries} 次仍受限，建议稍后重试")

        return False

    def signup(self, email: str, password: str, verify_code: str, max_retries: int = 3) -> bool:
        """双通道注册 + 自动换节点重试"""
        email = email.strip().lower()
        for attempt in range(1, max_retries + 1):
            try:
                url = f"{self.narra_base}/api/auth/signup"
                resp = self.session.post(url, json={"email": email, "password": password, "verify_code": verify_code.strip()}, timeout=8)
                if resp.status_code == 200 and resp.json().get("success"):
                    return True
            except Exception:
                pass

            last_err = ""
            try:
                with httpx.Client(**self._get_httpx_kwargs()) as client:
                    resp = client.post(
                        f"{self.netmind_auth}/register/registerUser",
                        data={
                            "email": email,
                            "password": password,
                            "verifyCode": verify_code.strip(),
                            "ckType": 2,
                            "subscribeFlag": 1
                        },
                        headers={"Content-Type": "application/x-www-form-urlencoded"}
                    )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("success"):
                        return True
                    last_err = str(data.get("msg") or data.get("errorcode") or "")
                else:
                    last_err = f"HTTP {resp.status_code}"
            except Exception as e:
                last_err = str(e)

            if attempt < max_retries:
                self.rotate_clash_proxy()
                time.sleep(2.0)
            else:
                raise Exception(f"注册账号失败: {last_err}")
        return False

    def login_netmind(self, email: str, password: str, max_retries: int = 3) -> str:
        """NetMind DES 加密登录，获取 loginToken (支持自动切节点重试)"""
        sign_str = self._random_sign_str(8)
        enc_pwd = self._des_cbc_encrypt(password, sign_str)
        payload = {
            "deviceId": 123231,
            "clientType": 5,
            "clientVersion": "1.0.0",
            "sysCode": "f925fc2c",
            "email": email.strip().lower(),
            "password": enc_pwd,
            "signStr": sign_str,
            "ckType": 2
        }
        url = f"{self.netmind_auth}/user/emailLogin"

        for attempt in range(1, max_retries + 1):
            try:
                with httpx.Client(**self._get_httpx_kwargs()) as client:
                    resp = client.post(url, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"})
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("success"):
                        login_token = data.get("data", {}).get("loginToken")
                        if login_token:
                            return login_token
                    err_msg = data.get("msg") or data.get("errorcode") or "login refused"
                else:
                    err_msg = f"HTTP {resp.status_code}"
            except Exception as e:
                err_msg = str(e)

            if attempt < max_retries:
                self.rotate_clash_proxy()
                time.sleep(2.0)
            else:
                raise Exception(f"NetMind login failed: {err_msg}")
        raise Exception("NetMind login failed after retries")

    def exchange_narra_token(self, login_token: str) -> Dict[str, Any]:
        """使用 NetMind loginToken 换取 NarraNexus 凭据与 user_id"""
        url = f"{self.narra_base}/api/auth/netmind-login"
        resp = self.session.post(url, json={"netmind_token": login_token}, timeout=15)
        if resp.status_code != 200:
            raise Exception(f"Exchange Narra token failed ({resp.status_code}): {resp.text}")
        
        data = resp.json()
        if not data.get("success"):
            raise Exception(f"Narra login refused: {data}")
        return data

    def get_agents(self, token: str, user_id: str) -> List[Dict[str, Any]]:
        """获取用户的伴随 Agent 列表"""
        url = f"{self.narra_base}/api/auth/agents"
        headers = {"Authorization": f"Bearer {token}", "X-User-Id": user_id}
        resp = self.session.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("agents", [])
        return []

    def get_quota(self, token: str, user_id: str) -> Dict[str, Any]:
        """获取当前账号的额度信息"""
        url = f"{self.narra_base}/api/quota/me"
        headers = {"Authorization": f"Bearer {token}", "X-User-Id": user_id}
        resp = self.session.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        return {}