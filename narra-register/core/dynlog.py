# -*- coding: utf-8 -*-
"""动态日志管理: 运行时实时切换日志级别与开关, 无需重启。

通道设计:
1. 业务 root logger setLevel -> 控制业务日志(INFO/DEBUG等)
2. 第三方库隔离: httpx/httpcore/uvicorn.error 等底层库强制压制在 WARNING,
   即使业务调到 DEBUG 也不会被底层 http 调试信息刷屏
3. attach 的 logger 直接 setLevel -> 控制 propagate=False 的日志
"""
import logging

_LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING,
           "ERROR": logging.ERROR, "CRITICAL": logging.CRITICAL}
_OFF = logging.CRITICAL + 50

# 第三方底层库: 永远压制在 WARNING(它们DEBUG时会海量刷屏, 淹没业务日志)
_NOISY_LIBS = ("httpx", "httpcore", "uvicorn.error", "urllib3", "asyncio",
               "anyio", "httpcore.connection", "httpcore.http11")


class DynLog:
    """集中式动态日志控制"""
    def __init__(self):
        self.loggers = {}
        self._pending = (True, "INFO")
        self._root = logging.getLogger()
        self._noisy = {}
        self._silence_noisy()
        self._apply(*self._pending)

    def _silence_noisy(self):
        """把第三方底层库 logger 固定到 WARNING, 存起原对象便于恢复"""
        for name in _NOISY_LIBS:
            lg = logging.getLogger(name)
            lg.setLevel(logging.WARNING)
            self._noisy[name] = lg

    def _resolve(self, enabled, level_name):
        return _LEVELS.get(level_name, logging.INFO) if enabled else _OFF

    def _apply(self, enabled, level_name):
        lvl = self._resolve(enabled, level_name)
        self._root.setLevel(lvl)
        for lg in self.loggers.values():
            try:
                lg.setLevel(lvl)
            except Exception:
                pass

    @property
    def scopes(self):
        return list(self.loggers.keys())

    def attach(self, scope, logger):
        self.loggers[scope] = logger
        en, lv = self._pending
        try:
            logger.setLevel(self._resolve(en, lv))
        except Exception:
            pass

    def apply(self, enabled, level_name):
        enabled = bool(enabled)
        level_name = str(level_name).upper()
        if level_name not in _LEVELS:
            level_name = "INFO"
        self._pending = (enabled, level_name)
        self._apply(enabled, level_name)


dynlog = DynLog()


def get_dynlevel() -> str:
    lvl = dynlog._root.level
    if lvl >= _OFF:
        return "OFF"
    for name, v in _LEVELS.items():
        if v == lvl:
            return name
    return "INFO"
