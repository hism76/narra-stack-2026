#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NarraNexus 总管理控制台 Web 服务启动器
"""

import os
import sys
import uvicorn
import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s %(levelname)s] %(message)s")

if __name__ == "__main__":
    import sqlite3
    from core.dynlog import dynlog
    try:
        c = sqlite3.connect("/app/data/narra.db")
        en = c.execute("SELECT value FROM settings WHERE key=?", ("log_enabled",)).fetchone()
        lv = c.execute("SELECT value FROM settings WHERE key=?", ("log_level",)).fetchone()
        c.close()
        dynlog.apply(en[0] != "false" if en else True, lv[0] if lv else "INFO")
    except Exception as e:
        print("[DynLog] init fallback INFO:", e)
        dynlog.apply(True, "INFO")
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")
    print(f"[*] 启动 NarraNexus 总管理后台: http://127.0.0.1:{port}")
    uvicorn.run("web.app:app", host=host, port=port, reload=False)
