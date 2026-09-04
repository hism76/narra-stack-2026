#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OmniBot 批量注册 CLI 工具 v3.0 (集成 YYDS Mail 自动接码与本地账号管理)

支持模式：
  1. 自动生成模式 (--auto <数量>): 自动调用 YYDS Mail 选域开箱，原子长轮询取码注册
  2. 文本列表模式 (<accounts.txt>): 从文本读取已有邮箱，支持人工或文件预填验证码
  3. Token 活性校验模式 (--verify): 重新校验库内所有已保存 Token 的可用状态
  4. 账号池导出模式 (--export [csv|json|flow2api]): 格式化导出账号池供下游直接调用

环境变量：
  YYDS_MAIL_API_KEY          YYDS Mail API Key (X-API-Key: AC-... 或 Token)
  YYDS_MAIL_BASE_URL         YYDS Mail 接口基址 (默认 https://maliapi.215.im/v1)
  OMNIBOT_BASE_URL           OmniBot 接口基址 (默认 https://account.omnimind.com.cn)
  OMNIBOT_DEFAULT_PASSWORD   默认注册密码 (留空则读取设置)
  OMNIBOT_PROXY              HTTP/HTTPS 代理 (如 http://127.0.0.1:7890)
  OMNIBOT_CSV                凭据输出 (默认 tokens.csv)
"""

import argparse
import datetime
import json
import os
import random
import sys
import time

from core.database import AccountDatabase
from core.engine import RegisterTaskRunner
from core.omnibot_auth import OmniBotClient
from core.yyds_mail import YYDSMailClient


def get_env_or_default(key: str, default: str) -> str:
    return os.environ.get(key, default).strip()


def export_tokens(db: AccountDatabase, fmt: str, output_path: str = ""):
    accounts = db.get_all_accounts()
    if fmt == "csv":
        path = output_path or "tokens.csv"
        db.export_to_csv(path)
        print(f"[+] 已将 {len(accounts)} 个账号导出至 CSV: {path}")
    elif fmt == "json":
        path = output_path or "tokens.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(accounts, f, ensure_ascii=False, indent=2)
        print(f"[+] 已将 {len(accounts)} 个账号导出至 JSON: {path}")
    elif fmt == "flow2api":
        path = output_path or "flow2api_tokens.txt"
        valid_tokens = [a["access_token"] for a in accounts if a.get("access_token") and a.get("status") == "已校验"]
        with open(path, "w", encoding="utf-8") as f:
            for t in valid_tokens:
                f.write(t + "\n")
        print(f"[+] 已将 {len(valid_tokens)} 个有效 Token 导出至 flow2api 格式: {path}")


def verify_all_accounts(db: AccountDatabase, omni: OmniBotClient):
    accounts = db.get_all_accounts()
    print(f"[*] 开始校验数据库中 {len(accounts)} 个账号的 Token 活性...")
    valid_c = 0
    for idx, acc in enumerate(accounts, 1):
        email = acc["email"]
        token = acc.get("access_token", "")
        if not token:
            print(f"[{idx}/{len(accounts)}] {email} -> 无 Token")
            continue
        ok, uid, _ = omni.verify_token(token)
        new_status = "已校验" if ok else "已失效"
        if ok:
            valid_c += 1
        acc["status"] = new_status
        db.save_account(acc)
        print(f"[{idx}/{len(accounts)}] {email} -> 校验结果: {new_status} (UID: {uid})")
    print(f"[+] 校验完成：有效 {valid_c} / 共 {len(accounts)}")


def main():
    parser = argparse.ArgumentParser(description="OmniBot 账号自动批量注册与管理工具 v3.0")
    parser.add_argument("accounts_file", nargs="?", help="账号文件路径 (如 accounts.txt)")
    parser.add_argument("--auto", type=int, metavar="COUNT", help="全自动无人值守注册指定数量账号 (使用 YYDS Mail)")
    parser.add_argument("--key", default=os.environ.get("YYDS_MAIL_API_KEY", ""), help="YYDS Mail API Key")
    parser.add_argument("--password", default=os.environ.get("OMNIBOT_DEFAULT_PASSWORD", ""), help="指定注册密码(留空则读取设置 default_account_password)")
    parser.add_argument("--proxy", default=os.environ.get("OMNIBOT_PROXY", ""), help="HTTP/HTTPS 代理")
    parser.add_argument("--verify", action="store_true", help="校验所有已保存账号的 Token 有效性")
    parser.add_argument("--export", choices=["csv", "json", "flow2api"], help="导出账号凭据池")
    parser.add_argument("--output", default="", help="导出目标文件路径")

    args = parser.parse_args()
    db = AccountDatabase()

    yyds_key = args.key or db.get_setting("yyds_mail_api_key") or os.environ.get("YYDS_MAIL_API_KEY", "")
    yyds_client = YYDSMailClient(api_key=yyds_key)
    omni_client = OmniBotClient(default_password=args.password, proxy=args.proxy if args.proxy else None)
    runner = RegisterTaskRunner(yyds_client=yyds_client, omnibot_client=omni_client, db=db)

    # 1. 导出模式
    if args.export:
        export_tokens(db, args.export, args.output)
        return

    # 2. 批量校验模式
    if args.verify:
        verify_all_accounts(db, omni_client)
        return

    # 3. 全自动模式
    if args.auto:
        if not yyds_key:
            print("[!] 错误: 全自动注册需要 YYDS Mail API Key！")
            print("    请通过 --key 参数提供，或设置环境变量 YYDS_MAIL_API_KEY=AC-xxx")
            sys.exit(1)
        runner.run_batch_auto(
            count=args.auto,
            custom_password=args.password,
        )
        return

    # 4. 指定文件注册模式 (兼容旧版)
    if args.accounts_file:
        if not os.path.exists(args.accounts_file):
            print(f"[!] 账号文件不存在: {args.accounts_file}")
            sys.exit(1)
        # 调用兼容的经典模式逻辑
        from omni_batch_register_compat import run_compat_mode
        run_compat_mode(args.accounts_file, args.password, omni_client, db)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
