import os
import asyncio
import json
import logging
import time
import uuid
from collections import deque
from typing import Optional, Dict, List, Any, AsyncGenerator, Union
import websockets
from fastapi import APIRouter, HTTPException, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from core.database import AccountDatabase
from schemas import ChatCompletionRequest, ResponseRequest

logger = logging.getLogger("async_gateway")
router = APIRouter(tags=["OpenAI Gateway"])

WS_URL = "wss://agent.narra.nexus/ws/agent/run"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OFFICIAL_MODELS_PATH = os.path.join(BASE_DIR, "official_models.json")

def load_official_models() -> List[str]:
    if os.path.exists(OFFICIAL_MODELS_PATH):
        try:
            with open(OFFICIAL_MODELS_PATH, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load official_models.json: {e}")
    return ["narra-agent", "narra-nexus", "gpt-4o", "claude-3-5-sonnet", "deepseek-v3", "deepseek-r1"]

db_gw = AccountDatabase()

class APIKeyStore:
    def __init__(self, db: AccountDatabase):
        self.db = db
        self._keys: Dict[str, Dict[str, Any]] = {}
        self._usage: Dict[str, Dict[str, int]] = {}
        self._windows: Dict[str, deque] = {}
        self.load()

    def load(self):
        raw = self.db.get_setting("api_keys", "")
        keys: Dict[str, Dict[str, Any]] = {}
        if raw:
            try:
                for item in json.loads(raw):
                    k = str(item.get("key", "")).strip().removeprefix("sk-")
                    if k:
                        keys[k] = {"name": item.get("name", "unnamed"), "rpm": int(item.get("rpm", 0))}
            except Exception as e:
                logger.error(f"[KeyStore] api_keys 配置解析失败: {e}")
        self._keys = keys

    def reload(self):
        self.load()

    def verify(self, authorization: Optional[str]) -> Dict[str, Any]:
        if not self._keys:
            return {"key": "default", "name": "default", "rpm": 0}
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Missing bearer key")
        bare = authorization[7:].strip().removeprefix("sk-")
        info = self._keys.get(bare)
        if info is None:
            raise HTTPException(status_code=401, detail="Invalid API key")
        return {"key": bare, "name": info["name"], "rpm": info["rpm"]}

    def record(self, key: str, success: bool = True):
        pass

    async def aflush(self):
        pass

keys_store = APIKeyStore(db_gw)

class GatewayCircuit:
    def configure(self, *args, **kwargs):
        pass
    def is_open(self) -> bool:
        return False

class GatewayPool:
    def __init__(self):
        self.circuit = GatewayCircuit()
        self._tokens = {}
        self._lock = asyncio.Lock()
    def invalidate(self, email: str):
        pass
    def invalidate_cached(self, email: str):
        pass
    def seed_cached(self, email: str, *args, **kwargs):
        pass

pool = GatewayPool()

def start_background_tasks():
    pass

@router.get("/v1/models")
@router.get("/models")
async def list_models():
    models = load_official_models()
    data = []
    now = int(time.time())
    for m in models:
        data.append({
            "id": m,
            "object": "model",
            "created": now,
            "owned_by": "narra-nexus",
            "permission": [],
            "root": m,
            "parent": None
        })
    return {"object": "list", "data": data}

def format_messages_to_prompt(messages: List[Dict[str, Any]]) -> str:
    if not messages:
        return ""
    if len(messages) == 1:
        return str(messages[0].get("content", ""))
    lines = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            lines.append(f"[System instruction: {content}]")
        elif role == "user":
            lines.append(f"User: {content}")
        elif role == "assistant":
            lines.append(f"Assistant: {content}")
    return "\n\n".join(lines)

def _pick_active_accounts(db: AccountDatabase, count: int = 5) -> List[Dict[str, Any]]:
    accounts = db.get_valid_accounts_pool(limit=count * 3)
    if not accounts:
        db.revive_cooling_accounts()
        accounts = db.get_valid_accounts_pool(limit=count * 3)
        if not accounts:
            raise HTTPException(status_code=503, detail="No active NarraNexus accounts available in token pool")
    import random
    random.shuffle(accounts)
    return accounts[:count]

def _resolve_agent_id(acc: Dict[str, Any]) -> str:
    token = acc.get("access_token")
    user_id = acc.get("user_id")
    notes = acc.get("notes") or "{}"
    agent_id = ""
    try:
        notes_data = json.loads(notes)
        agent_id = notes_data.get("agent_id", "")
    except Exception:
        pass
    if not agent_id:
        from core.narra_auth import NarraNexusClient
        client = NarraNexusClient()
        agents = client.get_agents(token, user_id)
        if agents:
            agent_id = agents[0].get("agent_id")
    return agent_id or "agent_default"

def _ensure_valid_account_token(acc: Dict[str, Any], db: AccountDatabase) -> tuple:
    token = acc.get("access_token")
    user_id = acc.get("user_id")
    refresh_token = acc.get("refresh_token")
    email = acc.get("email")
    agent_id = _resolve_agent_id(acc)
    if refresh_token and (not token or len(token) < 50):
        try:
            from core.narra_auth import NarraNexusClient
            client = NarraNexusClient()
            refreshed = client.exchange_narra_token(refresh_token)
            new_jwt = refreshed.get("token")
            if new_jwt:
                token = new_jwt
                acc["access_token"] = new_jwt
                db.mark_account_active(email, new_token=new_jwt)
        except Exception:
            pass
    return token, user_id, agent_id

def _extract_request_params(req: Any, db: AccountDatabase) -> Dict[str, Any]:
    effort = None
    if hasattr(req, "reasoning_effort") and req.reasoning_effort:
        effort = req.reasoning_effort
    elif hasattr(req, "reasoning") and isinstance(req.reasoning, dict):
        effort = req.reasoning.get("effort")
    elif isinstance(req, dict):
        effort = req.get("reasoning_effort") or (req.get("reasoning") or {}).get("effort")

    default_effort = db.get_setting("default_reasoning_effort", "medium")
    final_effort = effort or default_effort

    # Fast Mode 开关策略：请求体传参优先，未传时取系统全局设置 (默认 true)
    fast_mode = None
    if hasattr(req, "fast_mode") and req.fast_mode is not None:
        fast_mode = bool(req.fast_mode)
    elif isinstance(req, dict) and "fast_mode" in req:
        fast_mode = bool(req["fast_mode"])
    if fast_mode is None:
        fast_mode = db.get_setting("fast_mode_enabled", "true").lower() == "true"

    temperature = getattr(req, "temperature", None)
    if temperature is None and isinstance(req, dict):
        temperature = req.get("temperature")
    if temperature is None:
        temperature = 0.7

    top_p = getattr(req, "top_p", None)
    if top_p is None and isinstance(req, dict):
        top_p = req.get("top_p")
    if top_p is None:
        top_p = 1.0

    max_tokens = getattr(req, "max_tokens", None) or getattr(req, "max_completion_tokens", None) or getattr(req, "max_output_tokens", None)
    if max_tokens is None and isinstance(req, dict):
        max_tokens = req.get("max_tokens") or req.get("max_completion_tokens") or req.get("max_output_tokens")

    tools = getattr(req, "tools", None)
    if tools is None and isinstance(req, dict):
        tools = req.get("tools")

    tool_choice = getattr(req, "tool_choice", None)
    if tool_choice is None and isinstance(req, dict):
        tool_choice = req.get("tool_choice")

    response_format = getattr(req, "response_format", None)
    if response_format is None and isinstance(req, dict):
        response_format = req.get("response_format")

    return {
        "reasoning_effort": final_effort,
        "reasoning": {"effort": final_effort},
        "fast_mode": fast_mode,
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": max_tokens,
        "tools": tools,
        "tool_choice": tool_choice,
        "response_format": response_format
    }

# =========================================================================
# 对话补全接口 (/v1/chat/completions) 带无感 Failover 与保活
# =========================================================================
@router.post("/v1/chat/completions")
@router.post("/chat/completions")
async def chat_completions(request: ChatCompletionRequest, req: Request):
    global db_gw
    if db_gw is None:
        db_gw = AccountDatabase()

    auth_hdr = req.headers.get("Authorization")
    if keys_store._keys:
        keys_store.verify(auth_hdr)

    candidate_accounts = _pick_active_accounts(db_gw, count=5)
    input_prompt = format_messages_to_prompt([m.model_dump() if hasattr(m, "model_dump") else m for m in request.messages])
    model_name = request.model or "narra-agent"
    stream = bool(request.stream)
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:16]}"
    created_ts = int(time.time())
    params = _extract_request_params(request, db_gw)

    async def stream_generator() -> AsyncGenerator[str, None]:
        ws = None
        first_chunk_sent = False
        
        for acc in candidate_accounts:
            token, user_id, agent_id = _ensure_valid_account_token(acc, db_gw)

            ws_payload = {
                "token": token,
                "agent_id": agent_id,
                "user_id": user_id,
                "input_content": input_prompt,
                "model": model_name,
                "reasoning_effort": params["reasoning_effort"],
                "reasoning": params["reasoning"],
                "temperature": params["temperature"],
                "top_p": params["top_p"],
            }
            if params.get("fast_mode"):
                ws_payload["fast_mode"] = True
            if params["max_tokens"] is not None:
                ws_payload["max_tokens"] = params["max_tokens"]
                ws_payload["max_completion_tokens"] = params["max_tokens"]
            if params["tools"]:
                ws_payload["tools"] = params["tools"]
            if params["tool_choice"]:
                ws_payload["tool_choice"] = params["tool_choice"]
            if params["response_format"]:
                ws_payload["response_format"] = params["response_format"]

            account_error = None
            try:
                ws = await asyncio.wait_for(websockets.connect(WS_URL), timeout=10)
                await ws.send(json.dumps(ws_payload))

                while True:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
                        continue

                    data = json.loads(msg)
                    msg_type = data.get("type")

                    if msg_type in ("heartbeat", "progress"):
                        yield ": keep-alive\n\n"
                        continue

                    if msg_type in ("agent_response", "agent_reply_delta"):
                        delta_text = data.get("delta", "")
                        if delta_text:
                            chunk = {
                                "id": chat_id,
                                "object": "chat.completion.chunk",
                                "created": created_ts,
                                "model": model_name,
                                "fast_mode": params["fast_mode"],
                                "choices": [{"index": 0, "delta": {"content": delta_text}, "finish_reason": None}]
                            }
                            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                            first_chunk_sent = True

                    elif msg_type == "agent_thinking":
                        delta_think = data.get("delta", "")
                        if delta_think:
                            chunk = {
                                "id": chat_id,
                                "object": "chat.completion.chunk",
                                "created": created_ts,
                                "model": model_name,
                                "fast_mode": params["fast_mode"],
                                "choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": delta_think}, "finish_reason": None}]
                            }
                            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                            first_chunk_sent = True

                    elif msg_type == "complete":
                        stop_chunk = {
                            "id": chat_id,
                            "object": "chat.completion.chunk",
                            "created": created_ts,
                            "model": model_name,
                            "fast_mode": params["fast_mode"],
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
                        }
                        yield f"data: {json.dumps(stop_chunk)}\n\n"
                        yield "data: [DONE]\n\n"
                        db_gw.record_account_success(acc["email"])
                        return

                    elif msg_type == "error":
                        account_error = data.get("error_message") or "Narra Agent error"
                        err_code = data.get("error_code")
                        if err_code == "account_suspended" or "not available" in account_error.lower():
                            db_gw.mark_account_dead(acc["email"], reason="Account suspended")
                        else:
                            db_gw.record_account_failure(acc["email"], fail_reason=account_error)
                        break

            except Exception as e:
                account_error = str(e)
                logger.warning(f"Account {acc['email']} connection fail: {e}")
                db_gw.record_account_failure(acc["email"], fail_reason=account_error)
            finally:
                if ws is not None:
                    try: await ws.close()
                    except Exception: pass

            if first_chunk_sent:
                break

        if not first_chunk_sent:
            err_payload = {"error": {"message": f"All candidate accounts failed: {account_error}", "type": "server_error", "code": 500}}
            yield f"data: {json.dumps(err_payload)}\n\n"

    if stream:
        return StreamingResponse(
            stream_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"
            }
        )

    # 非流式 Failover
    last_err = ""
    for acc in candidate_accounts:
        token, user_id, agent_id = _ensure_valid_account_token(acc, db_gw)

        ws_payload = {
            "token": token,
            "agent_id": agent_id,
            "user_id": user_id,
            "input_content": input_prompt,
            "model": model_name,
            "reasoning_effort": params["reasoning_effort"],
            "reasoning": params["reasoning"],
            "temperature": params["temperature"],
            "top_p": params["top_p"],
        }
        if params.get("fast_mode"):
            ws_payload["fast_mode"] = True
        if params["max_tokens"] is not None:
            ws_payload["max_tokens"] = params["max_tokens"]
            ws_payload["max_completion_tokens"] = params["max_tokens"]
        if params["tools"]:
            ws_payload["tools"] = params["tools"]
        if params["tool_choice"]:
            ws_payload["tool_choice"] = params["tool_choice"]
        if params["response_format"]:
            ws_payload["response_format"] = params["response_format"]

        ws = None
        full_content = []
        full_thinking = []
        try:
            ws = await asyncio.wait_for(websockets.connect(WS_URL), timeout=10)
            await ws.send(json.dumps(ws_payload))

            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=60)
                data = json.loads(msg)
                msg_type = data.get("type")
                if msg_type in ("agent_response", "agent_reply_delta"):
                    full_content.append(data.get("delta", ""))
                elif msg_type == "agent_thinking":
                    full_thinking.append(data.get("delta", ""))
                elif msg_type == "complete":
                    db_gw.record_account_success(acc["email"])
                    content_str = "".join(full_content)
                    thinking_str = "".join(full_thinking)
                    msg_dict = {"role": "assistant", "content": content_str}
                    if thinking_str:
                        msg_dict["reasoning_content"] = thinking_str
                    return JSONResponse(content={
                        "id": chat_id,
                        "object": "chat.completion",
                        "created": created_ts,
                        "model": model_name,
                        "fast_mode": params["fast_mode"],
                        "choices": [{"index": 0, "message": msg_dict, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": len(input_prompt), "completion_tokens": len(content_str) + len(thinking_str), "total_tokens": len(input_prompt) + len(content_str) + len(thinking_str)}
                    })
                elif msg_type == "error":
                    err_text = data.get("error_message") or "Agent error"
                    err_code = data.get("error_code")
                    if err_code == "account_suspended" or "not available" in err_text.lower():
                        db_gw.mark_account_dead(acc["email"], reason="Account suspended")
                    raise Exception(err_text)
        except Exception as e:
            last_err = str(e)
            logger.warning(f"Failover trigger on {acc['email']}: {e}")
            db_gw.record_account_failure(acc["email"], fail_reason=last_err)
        finally:
            if ws is not None:
                try: await ws.close()
                except Exception: pass

    raise HTTPException(status_code=500, detail=f"Inference error: {last_err}")

# =========================================================================
# Responses API (/v1/responses) 带无感 Failover 与保活
# =========================================================================
def _extract_response_input(req: ResponseRequest) -> str:
    parts = []
    if req.instructions:
        parts.append(f"[Instruction: {req.instructions}]")
    inp = req.input
    if isinstance(inp, str):
        parts.append(inp)
    elif isinstance(inp, list):
        for item in inp:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                role = item.get("role", "user")
                c = item.get("content", "")
                if isinstance(c, list):
                    text_parts = [p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text"]
                    c = " ".join(text_parts)
                parts.append(f"{role}: {c}")
    return "\n\n".join(parts)

@router.post("/v1/responses")
@router.post("/v1/response")
@router.post("/responses")
@router.post("/response")
async def responses_api(req: ResponseRequest, request: Request):
    global db_gw
    if db_gw is None:
        db_gw = AccountDatabase()

    auth_hdr = request.headers.get("Authorization")
    if keys_store._keys:
        keys_store.verify(auth_hdr)

    candidate_accounts = _pick_active_accounts(db_gw, count=5)
    input_prompt = _extract_response_input(req)
    model_name = req.model or "narra-agent"
    stream = bool(req.stream)
    resp_id = f"resp_{uuid.uuid4().hex}"
    msg_id = f"msg_{uuid.uuid4().hex}"
    created_ts = int(time.time())
    params = _extract_request_params(req, db_gw)

    async def stream_responses_generator() -> AsyncGenerator[str, None]:
        yield ": keep-alive\n\n"
        ws = None
        full_text = []
        full_thinking = []
        first_event_sent = False
        for acc in candidate_accounts:
            token, user_id, agent_id = _ensure_valid_account_token(acc, db_gw)
            ws_payload = {
                "token": token,
                "agent_id": agent_id,
                "user_id": user_id,
                "input_content": input_prompt,
                "model": model_name,
                "reasoning_effort": params["reasoning_effort"],
                "reasoning": params["reasoning"],
                "temperature": params["temperature"],
                "top_p": params["top_p"],
            }
            if params.get("fast_mode"):
                ws_payload["fast_mode"] = True
                ws_payload["fastMode"] = True
            if params["max_tokens"] is not None:
                ws_payload["max_tokens"] = params["max_tokens"]
                ws_payload["max_output_tokens"] = params["max_tokens"]
            if params["tools"]:
                ws_payload["tools"] = params["tools"]
            if params["tool_choice"]:
                ws_payload["tool_choice"] = params["tool_choice"]
            if params["response_format"]:
                ws_payload["response_format"] = params["response_format"]
            account_error = None
            try:
                ws = await asyncio.wait_for(websockets.connect(WS_URL), timeout=10)
                await ws.send(json.dumps(ws_payload))
                while True:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    data = json.loads(msg)
                    msg_type = data.get("type")
                    if msg_type in ("heartbeat", "progress"):
                        yield ": keep-alive\n\n"
                        continue
                    if not first_event_sent and msg_type in ("agent_response", "agent_reply_delta", "agent_thinking"):
                        created_event = {
                            "type": "response.created",
                            "response": {
                                "id": resp_id,
                                "object": "response",
                                "created_at": created_ts,
                                "status": "in_progress",
                                "model": model_name,
                                "fast_mode": params["fast_mode"]
                            }
                        }
                        yield f"event: response.created\ndata: {json.dumps(created_event, ensure_ascii=False)}\n\n"
                        output_added_event = {
                            "type": "response.output_item.added",
                            "response_id": resp_id,
                            "output_index": 0,
                            "item": {
                                "id": msg_id,
                                "type": "message",
                                "status": "in_progress",
                                "role": "assistant",
                                "content": []
                            }
                        }
                        yield f"event: response.output_item.added\ndata: {json.dumps(output_added_event, ensure_ascii=False)}\n\n"
                        content_part_event = {
                            "type": "response.content_part.added",
                            "response_id": resp_id,
                            "item_id": msg_id,
                            "output_index": 0,
                            "content_index": 0,
                            "part": {"type": "text", "text": ""}
                        }
                        yield f"event: response.content_part.added\ndata: {json.dumps(content_part_event, ensure_ascii=False)}\n\n"
                        first_event_sent = True

                    if msg_type == "agent_thinking":
                        delta_think = data.get("delta", "")
                        if delta_think:
                            full_thinking.append(delta_think)
                            think_event = {
                                "type": "response.reasoning_text.delta",
                                "response_id": resp_id,
                                "item_id": msg_id,
                                "output_index": 0,
                                "content_index": 0,
                                "delta": delta_think
                            }
                            yield f"event: response.reasoning_text.delta\ndata: {json.dumps(think_event, ensure_ascii=False)}\n\n"

                    elif msg_type in ("agent_response", "agent_reply_delta"):
                        delta_text = data.get("delta", "")
                        if delta_text:
                            full_text.append(delta_text)
                            delta_event = {
                                "type": "response.output_text.delta",
                                "response_id": resp_id,
                                "item_id": msg_id,
                                "output_index": 0,
                                "content_index": 0,
                                "delta": delta_text
                            }
                            yield f"event: response.output_text.delta\ndata: {json.dumps(delta_event, ensure_ascii=False)}\n\n"

                    elif msg_type == "complete":
                        final_str = "".join(full_text)
                        done_text_event = {
                            "type": "response.text.done",
                            "response_id": resp_id,
                            "item_id": msg_id,
                            "output_index": 0,
                            "content_index": 0,
                            "text": final_str
                        }
                        yield f"event: response.text.done\ndata: {json.dumps(done_text_event, ensure_ascii=False)}\n\n"
                        output_item_done_event = {
                            "type": "response.output_item.done",
                            "response_id": resp_id,
                            "output_index": 0,
                            "item": {
                                "id": msg_id,
                                "type": "message",
                                "status": "completed",
                                "role": "assistant",
                                "content": [{"type": "text", "text": final_str}]
                            }
                        }
                        yield f"event: response.output_item.done\ndata: {json.dumps(output_item_done_event, ensure_ascii=False)}\n\n"
                        in_toks = max(1, len(input_prompt))
                        out_toks = max(1, len(final_str) + len("".join(full_thinking)))
                        usage_dict = {
                            "input_tokens": in_toks,
                            "output_tokens": out_toks,
                            "total_tokens": in_toks + out_toks,
                            "prompt_tokens": in_toks,
                            "completion_tokens": out_toks
                        }
                        completed_event = {
                            "type": "response.completed",
                            "response": {
                                "id": resp_id,
                                "object": "response",
                                "created_at": created_ts,
                                "status": "completed",
                                "model": model_name,
                                "fast_mode": params["fast_mode"],
                                "output": [
                                    {
                                        "id": msg_id,
                                        "type": "message",
                                        "status": "completed",
                                        "role": "assistant",
                                        "content": [{"type": "text", "text": final_str}]
                                    }
                                ],
                                "usage": usage_dict
                            },
                            "usage": usage_dict
                        }
                        yield f"event: response.completed\ndata: {json.dumps(completed_event, ensure_ascii=False)}\n\n"
                        done_final_event = {
                            "type": "response.done",
                            "response": completed_event["response"],
                            "usage": usage_dict
                        }
                        yield f"event: response.done\ndata: {json.dumps(done_final_event, ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"
                        db_gw.record_account_success(acc["email"])
                        return
                    elif msg_type == "error":
                        account_error = data.get("error_message") or "Agent execution error"
                        err_code = data.get("error_code")
                        if err_code == "account_suspended" or "not available" in account_error.lower():
                            db_gw.mark_account_dead(acc["email"], reason="Account suspended")
                        logger.warning(f"Responses Failover on {acc['email']}: {account_error}")
                        db_gw.record_account_failure(acc["email"], fail_reason=account_error)
                        break
            except Exception as e:
                account_error = str(e)
                logger.warning(f"Responses Failover on {acc['email']}: {e}")
                db_gw.record_account_failure(acc["email"], fail_reason=account_error)
            finally:
                if ws is not None:
                    try: await ws.close()
                    except Exception: pass
            if first_event_sent:
                break
        if not first_event_sent:
            yield f"event: error\ndata: {json.dumps({'error': {'message': f'All candidate accounts failed: {account_error}', 'type': 'server_error'}})}\n\n"

    if stream:
        return StreamingResponse(
            stream_responses_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"
            }
        )

    # 非流式 Responses Failover
    last_err = ""
    for acc in candidate_accounts:
        token, user_id, agent_id = _ensure_valid_account_token(acc, db_gw)

        ws_payload = {
            "token": token,
            "agent_id": agent_id,
            "user_id": user_id,
            "input_content": input_prompt,
            "model": model_name,
            "reasoning_effort": params["reasoning_effort"],
            "reasoning": params["reasoning"],
            "temperature": params["temperature"],
            "top_p": params["top_p"],
        }
        if params.get("fast_mode"):
            ws_payload["fast_mode"] = True
        if params["max_tokens"] is not None:
            ws_payload["max_tokens"] = params["max_tokens"]
            ws_payload["max_output_tokens"] = params["max_tokens"]
        if params["tools"]:
            ws_payload["tools"] = params["tools"]
        if params["tool_choice"]:
            ws_payload["tool_choice"] = params["tool_choice"]
        if params["response_format"]:
            ws_payload["response_format"] = params["response_format"]

        ws = None
        full_content = []
        full_thinking = []
        try:
            ws = await asyncio.wait_for(websockets.connect(WS_URL), timeout=10)
            await ws.send(json.dumps(ws_payload))

            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=60)
                data = json.loads(msg)
                msg_type = data.get("type")
                if msg_type in ("agent_response", "agent_reply_delta"):
                    full_content.append(data.get("delta", ""))
                elif msg_type == "agent_thinking":
                    full_thinking.append(data.get("delta", ""))
                elif msg_type == "complete":
                    db_gw.record_account_success(acc["email"])
                    content_str = "".join(full_content)
                    thinking_str = "".join(full_thinking)
                    output_content = [{"type": "text", "text": content_str}]
                    if thinking_str:
                        output_content.insert(0, {"type": "reasoning", "text": thinking_str})
                    return JSONResponse(content={
                        "id": resp_id,
                        "object": "response",
                        "created_at": created_ts,
                        "status": "completed",
                        "model": model_name,
                        "fast_mode": params["fast_mode"],
                        "output": [{"id": msg_id, "type": "message", "status": "completed", "role": "assistant", "content": output_content}],
                        "usage": {"input_tokens": len(input_prompt), "output_tokens": len(content_str) + len(thinking_str), "total_tokens": len(input_prompt) + len(content_str) + len(thinking_str)}
                    })
                elif msg_type == "error":
                    err_text = data.get("error_message") or "Agent error"
                    err_code = data.get("error_code")
                    if err_code == "account_suspended" or "not available" in err_text.lower():
                        db_gw.mark_account_dead(acc["email"], reason="Account suspended")
                    raise Exception(err_text)
        except Exception as e:
            last_err = str(e)
            logger.warning(f"Responses non-stream Failover on {acc['email']}: {e}")
            db_gw.record_account_failure(acc["email"], fail_reason=last_err)
        finally:
            if ws is not None:
                try: await ws.close()
                except Exception: pass

    raise HTTPException(status_code=500, detail=f"Inference error: {last_err}")

@router.post("/v1/embeddings")
@router.post("/embeddings")
async def embeddings_api(req: Request):
    return JSONResponse(content={
        "object": "list",
        "data": [{"object": "embedding", "embedding": [0.0] * 1536, "index": 0}],
        "model": "text-embedding-3-small",
        "usage": {"prompt_tokens": 1, "total_tokens": 1}
    })