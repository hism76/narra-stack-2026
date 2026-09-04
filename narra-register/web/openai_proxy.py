import os
import json
import logging
import asyncio
import time
from typing import Optional, List, AsyncGenerator
from fastapi import APIRouter, HTTPException, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
import httpx

from schemas import ChatCompletionRequest, EmbeddingRequest, ModelList, ModelCard
from core.pool_manager import AccountPoolManager

router = APIRouter(tags=["OpenAI Compatible API"])
logger = logging.getLogger("openai_proxy")
from core.dynlog import dynlog  # noqa: E402
dynlog.attach("openai_proxy", logger)

UPSTREAM_BASE = "https://model-api.omnimind.com.cn"
pool_manager = AccountPoolManager()

_global_http_client: Optional[httpx.AsyncClient] = None

def get_http_client() -> httpx.AsyncClient:
    global _global_http_client
    if _global_http_client is None or _global_http_client.is_closed:
        _global_http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(180.0, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=100, max_connections=200, keepalive_expiry=30.0),
            trust_env=False,
            http2=False
        )
    return _global_http_client

PROXY_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
}

OFFICIAL_MODELS = [
    # DeepSeek 系列
    "deepseek-v4-pro-0813",
    "deepseek-v4-pro",
    "deepseek-v4-flash-0731",
    "deepseek-v4-flash",
    "deepseek-chat",
    "deepseek-v3",
    "deepseek-reasoner",

    # Qwen 系列 (文本/多模态/工具)
    "qwen3.7-plus",
    "Qwen3.5-Plus",
    "qwen-max",
    "qwen-plus",
    "qwen3-vl-plus",
    "qwen-image-2.0",
    "text-embedding-v4",
    "qwen3-tts-flash",
    "qwen3-asr-flash-2026-02-10",

    # 其它原生与通用系列
    "opus-6",
    "claude-3-5-sonnet",
    "gpt-4o",
    "gpt-4o-mini",
    "omnimind-chat"
]

def get_gateway_timeout() -> float:
    try:
        val = pool_manager.db.get_setting("request_timeout", "120")
        return float(val)
    except Exception:
        return 120.0

@router.get("/v1/models", response_model=ModelList)
@router.get("/models", response_model=ModelList)
async def list_models():
    cards = [
        ModelCard(
            id=m,
            object="model",
            created=1724140800,
            owned_by="omnimind-ai"
        )
        for m in OFFICIAL_MODELS
    ]
    return ModelList(object="list", data=cards)

async def sse_event_streamer(response: httpx.Response, email: Optional[str] = None, pool_mgr: Optional[AccountPoolManager] = None) -> AsyncGenerator[str, None]:
    """
    全双工 SSE 流式响应泵：
    1. 原生字节流无损透传；
    2. 支持客户端断开时主动切断上游连接 (aclose)；
    3. 结束或异常时必定释放账号租借锁 (release_lease)。
    """
    try:
        aiter = response.aiter_lines()
        while True:
            try:
                line = await asyncio.wait_for(aiter.__anext__(), timeout=15.0)
                if line:
                    yield f"{line}\n\n"
            except asyncio.TimeoutError:
                # 输出标准 SSE 注释行保活，合规客户端与 New-API 会静默忽略
                yield ": keep-alive\n\n"
            except StopAsyncIteration:
                break
    except (asyncio.CancelledError, GeneratorExit):
        logger.info("[Stream] 客户端主动断开连接，及时关闭上游流")
    except Exception as e:
        logger.warning(f"[Stream] 流式传输异常中断: {e}")
    finally:
        try:
            await response.aclose()
        except Exception:
            pass
        if email and pool_mgr:
            pool_mgr.release_lease(email)

@router.post("/v1/chat/completions")
@router.post("/chat/completions")
async def chat_completions(req: ChatCompletionRequest, authorization: Optional[str] = Header(None)):
    logger.info(f"[Proxy] 收到 ChatCompletion 请求: model={req.model}, stream={req.stream}")
    # 0. 检查平台级防雪崩熔断器
    if pool_manager.is_circuit_open():
        logger.warning("[Proxy] 触发平台防雪崩熔断保护，暂停服务 45s 以保护 400+ 账号")
        raise HTTPException(
            status_code=503,
            detail="上游模型服务出现大面积波动，已触发平台保护性熔断，请 30 秒后再试"
        )

    # 严格原样透传模型名称
    req_model = req.model
    is_stream = req.stream if req.stream is not None else False

    # 1. 基础推理参数透传（默认 temperature=1.0, top_p=1.0）
    payload = {
        "model": req_model,
        "messages": [m.dict(exclude_none=True) for m in req.messages],
        "stream": is_stream,
        "temperature": req.temperature if req.temperature is not None else 1.0,
        "top_p": req.top_p if req.top_p is not None else 1.0,
    }

    # 2. enable_thinking 参数处理（默认开启深度思考 True，Agent 可显式传 False 关闭）
    payload["enable_thinking"] = req.enable_thinking if req.enable_thinking is not None else True

    # 3. reasoning_effort 透传
    if req.reasoning_effort is not None:
        payload["reasoning_effort"] = req.reasoning_effort

    # 4. max_completion_tokens / max_tokens 透传
    target_completion_tokens = req.max_completion_tokens if req.max_completion_tokens is not None else req.max_tokens
    if target_completion_tokens is not None:
        payload["max_tokens"] = target_completion_tokens
        payload["max_completion_tokens"] = target_completion_tokens

    # 5. 高级生成控制参数透传 (惩罚项、停止词、种子等)
    if req.presence_penalty is not None:
        payload["presence_penalty"] = req.presence_penalty
    if req.frequency_penalty is not None:
        payload["frequency_penalty"] = req.frequency_penalty
    if req.stop is not None:
        payload["stop"] = req.stop
    if req.seed is not None:
        payload["seed"] = req.seed

    # 6. Function Calling / Tools 透传
    if req.tools is not None:
        payload["tools"] = req.tools
    if req.tool_choice is not None:
        payload["tool_choice"] = req.tool_choice

    # 7. 结构化输出 response_format (JSON Schema) 透传
    if req.response_format is not None:
        payload["response_format"] = req.response_format

    url = f"{UPSTREAM_BASE}/v1/chat/completions"
    max_retries = 3
    failed_emails: List[str] = []
    timeout_val = get_gateway_timeout()

    for attempt in range(max_retries):
        token, email, pwd = await asyncio.to_thread(pool_manager.checkout_account, exclude_emails=failed_emails)
        logger.info(f"[Proxy] 第 {attempt+1} 次检出账号: {email}, token存在: {bool(token)}")
        if not token or not email:
            raise HTTPException(
                status_code=503,
                detail="No active accounts currently available in pool. All accounts are in cooling or dead."
            )

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "OmniBot-Android/2.0",
            "Accept": "text/event-stream" if is_stream else "application/json"
        }

        try:
            client = get_http_client()
            logger.info(f"[Proxy] 正在向上游发送请求: {url} (model={req_model})")
            req_obj = client.build_request("POST", url, json=payload, headers=headers, timeout=timeout_val)
            response = await client.send(req_obj, stream=is_stream)
            logger.info(f"[Proxy] 上游响应状态码: {response.status_code} (email={email})")

            # 401 令牌过期：原地自愈重登并刷新 Token，仅消耗当前 attempt，零报错重试
            if response.status_code == 401:
                try:
                    await response.aclose()
                except Exception:
                    pass
                logger.warning(f"[Proxy] 账号 {email} Token 已过期 (401)，正在原地自愈重新登录...")
                new_token = await asyncio.to_thread(pool_manager.relogin_and_get_token, email, pwd)
                if new_token:
                    retry_headers = headers.copy()
                    retry_headers["Authorization"] = f"Bearer {new_token}"
                    retry_req = client.build_request("POST", url, json=payload, headers=retry_headers, timeout=timeout_val)
                    response = await client.send(retry_req, stream=is_stream)

            # 200 成功响应
            if response.status_code == 200:
                pool_manager.mark_success(email)
                if is_stream:
                    # 流式响应：在 sse_event_streamer 的 finally 中精准释放租借锁与上游连接
                    return StreamingResponse(
                        sse_event_streamer(response, email=email, pool_mgr=pool_manager),
                        media_type="text/event-stream",
                        headers=PROXY_HEADERS
                    )
                else:
                    try:
                        resp_data = response.json()
                        return JSONResponse(content=resp_data, headers=PROXY_HEADERS)
                    finally:
                        pool_manager.release_lease(email)

            # 403 / 404 / 400 账号状态失效或封禁 -> 标记死亡
            elif response.status_code in [400, 403, 404]:
                err_text = response.text
                logger.error(f"[Proxy] 账号 {email} 异常失效 ({response.status_code}): {err_text}")
                pool_manager.mark_dead(email, reason=f"HTTP {response.status_code}: {err_text[:100]}")
                pool_manager.release_lease(email)
                failed_emails.append(email)
                continue

            # 429 请求受限 -> 标记 120s 快速冷却
            elif response.status_code == 429:
                logger.warning(f"[Proxy] 账号 {email} 触发上游 429 限频，自动进入 120s 冷却池")
                pool_manager.mark_rate_limited(email, duration_seconds=120)
                pool_manager.release_lease(email)
                failed_emails.append(email)
                continue

            # 5xx 上游临时波动 -> 标记 45s 短暂冷却
            elif response.status_code >= 500:
                logger.warning(f"[Proxy] 上游服务 5xx 临时波动 ({response.status_code})，账号 {email} 冷却 45s")
                pool_manager.mark_rate_limited(email, duration_seconds=45)
                pool_manager.release_lease(email)
                failed_emails.append(email)
                continue

            else:
                logger.warning(f"[Proxy] 账号 {email} 返回未预期状态码 {response.status_code}: {response.text[:100]}")
                pool_manager.release_lease(email)
                failed_emails.append(email)
                continue

        except Exception as e:
            logger.warning(f"[Proxy] 请求账号 {email} 异常 (第 {attempt+1} 次): {type(e).__name__}: {e}")
            pool_manager.release_lease(email)
            failed_emails.append(email)
            continue

    raise HTTPException(status_code=502, detail="Upstream request failed after multiple retries across account pool")

@router.post("/v1/embeddings")
@router.post("/embeddings")
async def embeddings(req: EmbeddingRequest, authorization: Optional[str] = Header(None)):
    """OpenAI 兼容向量嵌入接口，支持 text-embedding-v4 知识库与 RAG 检索"""
    if pool_manager.is_circuit_open():
        raise HTTPException(status_code=503, detail="上游服务处于平台熔断保护期，请稍后再试")

    req_model = req.model or "text-embedding-v4"
    req_input = req.input

    payload = {
        "model": req_model,
        "input": req_input,
    }
    if req.user:
        payload["user"] = req.user

    url = f"{UPSTREAM_BASE}/v1/embeddings"
    max_retries = 3
    failed_emails: List[str] = []
    timeout_val = get_gateway_timeout()

    for attempt in range(max_retries):
        token, email, pwd = await asyncio.to_thread(pool_manager.checkout_account, exclude_emails=failed_emails)
        logger.info(f"[Proxy] 第 {attempt+1} 次检出账号: {email}, token存在: {bool(token)}")
        if not token or not email:
            raise HTTPException(
                status_code=503,
                detail="No active accounts available in pool for embeddings."
            )

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "OmniBot-Android/2.0",
        }

        try:
            client = get_http_client()
            req_obj = client.build_request("POST", url, json=payload, headers=headers, timeout=timeout_val)
            response = await client.send(req_obj)

            if response.status_code == 401:
                new_token = await asyncio.to_thread(pool_manager.relogin_and_get_token, email, pwd)
                if new_token:
                    headers["Authorization"] = f"Bearer {new_token}"
                    retry_req = client.build_request("POST", url, json=payload, headers=headers, timeout=timeout_val)
                    response = await client.send(retry_req)

            if response.status_code == 200:
                pool_manager.mark_success(email)
                pool_manager.release_lease(email)
                return JSONResponse(content=response.json(), headers=PROXY_HEADERS)
            elif response.status_code == 429:
                pool_manager.mark_rate_limited(email, duration_seconds=120)
                pool_manager.release_lease(email)
                failed_emails.append(email)
                continue
            else:
                pool_manager.release_lease(email)
                failed_emails.append(email)
                continue
        except Exception as e:
            logger.warning(f"[Proxy] Embeddings 请求账号 {email} 异常: {e}")
            pool_manager.release_lease(email)
            failed_emails.append(email)
            continue

    raise HTTPException(status_code=502, detail="Embeddings request failed across account pool")
