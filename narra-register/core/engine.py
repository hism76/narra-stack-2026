import time
import uuid
import random
import string
import logging
import json
from typing import Optional, Callable, Dict, Any, Tuple, List
from .database import AccountDatabase
from .yyds_mail import YYDSMailClient
from .narra_auth import NarraNexusClient

logger = logging.getLogger("RegistrationEngine")

class RegistrationEngine:
    def __init__(
        self,
        db: AccountDatabase,
        yyds_client: YYDSMailClient,
        narra_client: NarraNexusClient,
        proxy: Optional[str] = None,
        default_code_timeout: int = 60
    ):
        self.db = db
        self.yyds = yyds_client
        self.narra = narra_client
        self.proxy = proxy
        self.default_code_timeout = default_code_timeout
        self._is_stopped = False

    def stop(self):
        self._is_stopped = True

    def reset_stop_flag(self):
        self._is_stopped = False

    def is_stopped(self) -> bool:
        return self._is_stopped

    @staticmethod
    def _generate_random_name(prefix: str = "narra", length: int = 6) -> str:
        chars = string.ascii_lowercase + string.digits
        random_suffix = "".join(random.choices(chars, k=length))
        return f"{prefix}{random_suffix}"

    def register_single_auto(
        self,
        domain_strategy: str = "smart",
        platform_code: str = "narra",
        timeout_seconds: Optional[int] = None,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """单次自动注册完整流水线 (具备自动切节点重试 + 超时安全保底)"""
        def log(msg: str):
            logger.info(f"[AutoRegister] {msg}")
            if log_callback:
                try:
                    log_callback(msg)
                except Exception:
                    pass

        raw_timeout = timeout_seconds if (timeout_seconds and timeout_seconds > 0) else self.default_code_timeout
        # 针对第三方邮件投递延迟做最低安全保底 (至少20秒)，防止邮件刚发就被判超时
        actual_timeout = max(20, raw_timeout)
        if raw_timeout < 20:
            log(f"⚠️ 设定的验证码超时过短 ({raw_timeout}s)，已自动保底为 {actual_timeout}s 确保邮件正常投递")

        if self._is_stopped:
            return False, "任务已暂停/停止", None

        # 1. 申请临时邮箱
        log("开始向 YYDS 申请临时邮箱...")
        try:
            local_part = self._generate_random_name("user", 8)
            mail_data = self.yyds.create_account(local_part=local_part)
            email_address = mail_data.get("address")
            temp_token = mail_data.get("token")
            if not email_address:
                return False, "未能从 YYDS 申请到可用邮箱", None
            log(f"临时邮箱申请成功: {email_address}")
        except Exception as e:
            err = f"申请临时邮箱异常: {e}"
            log(err)
            return False, err, None

        if self._is_stopped:
            return False, "任务已停止", None

        # 2. 发送验证码 (支持双通道 + 遭遇频控自动切节点换新 IP 重试)
        log(f"请求发送验证码至 {email_address}...")
        try:
            self.narra.send_signup_code(email_address, max_retries=3, log_callback=log)
            log("验证码已成功触发发送")
        except Exception as e:
            err = f"触发验证码发送失败: {e}"
            log(err)
            return False, err, None

        if self._is_stopped:
            return False, "任务已停止", None

        # 3. 轮询提取验证码
        log(f"正在轮询 YYDS 提取验证码 (超时 {actual_timeout}s)...")
        code = self.yyds.wait_for_verification_code(
            email_address,
            temp_token=temp_token,
            timeout=actual_timeout,
            stop_checker=lambda: self._is_stopped
        )
        if not code:
            return False, f"收取验证码超时 ({actual_timeout}s) 或未匹配到有效验证码", None
        log(f"成功获取验证码: {code}")

        if self._is_stopped:
            return False, "任务已停止", None

        # 4. 生成符合安全策略的强密码
        password = self.narra.generate_compliant_password()

        # 5. 提交注册
        log(f"正在提交账号注册: {email_address}...")
        try:
            self.narra.signup(email_address, password, code, max_retries=3)
            log("NarraNexus 账号注册成功！")
        except Exception as e:
            err = f"注册账号失败: {e}"
            log(err)
            return False, err, None

        if self._is_stopped:
            return False, "任务已停止", None

        # 6. 登录 NetMind 获取 loginToken (支持自动切节点)
        log("正在登录 NetMind.AI 鉴权网关...")
        try:
            login_token = self.narra.login_netmind(email_address, password, max_retries=3)
            log("NetMind 鉴权登录成功")
        except Exception as e:
            err = f"登录 NetMind 失败: {e}"
            log(err)
            return False, err, None

        # 7. 换取 NarraNexus 凭证与 user_id
        log("正在换取 NarraNexus JWT 访问凭证...")
        try:
            narra_info = self.narra.exchange_narra_token(login_token)
            narra_jwt = narra_info.get("token")
            user_id = narra_info.get("user_id")
            log(f"换票成功: user_id={user_id}")
        except Exception as e:
            err = f"换取 NarraNexus 凭证失败: {e}"
            log(err)
            return False, err, None

        # 8. 获取 Agent 列表与配额
        agent_id = ""
        quota_info = {}
        try:
            agents = self.narra.get_agents(narra_jwt, user_id)
            if agents:
                agent_id = agents[0].get("agent_id", "")
                log(f"已绑定伴随 Agent: {agent_id} ({agents[0].get('name', '')})")
            quota_info = self.narra.get_quota(narra_jwt, user_id)
            remaining = quota_info.get("remaining", 3.0)
            log(f"初始化配额: ${remaining} USD")
        except Exception as e:
            logger.warning(f"获取附属信息非阻断异常: {e}")

        # 9. 存入数据库
        notes_dict = {
            "agent_id": agent_id,
            "quota": quota_info.get("remaining", 3.0),
            "currency": quota_info.get("currency", "USD")
        }
        self.db.add_account(
            email=email_address,
            token=narra_jwt,
            password=password,
            user_id=user_id,
            remark=json.dumps(notes_dict),
            refresh_token=login_token
        )
        log(f"账号 {email_address} 已成功入库激活！")

        account_result = {
            "email": email_address,
            "password": password,
            "user_id": user_id,
            "agent_id": agent_id,
            "access_token": narra_jwt,
            "refresh_token": login_token,
            "quota": quota_info.get("remaining", 3.0)
        }
        return True, "注册成功", account_result