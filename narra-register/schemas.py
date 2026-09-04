from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any, Union

class ChatMessage(BaseModel):
    role: str
    content: Optional[Union[str, List[Dict[str, Any]]]] = ""
    name: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    reasoning_content: Optional[str] = None

    class Config:
        extra = "allow"

class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    stream: Optional[bool] = False
    temperature: Optional[float] = 1.0
    top_p: Optional[float] = 1.0
    max_tokens: Optional[int] = None
    max_completion_tokens: Optional[int] = None
    reasoning_effort: Optional[str] = None
    enable_thinking: Optional[bool] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    stop: Optional[Union[str, List[str]]] = None
    seed: Optional[int] = None
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None
    response_format: Optional[Dict[str, Any]] = None
    fast_mode: Optional[bool] = None

class EmbeddingRequest(BaseModel):
    model: str = "text-embedding-v4"
    input: Union[str, List[str], List[int], List[List[int]]]
    user: Optional[str] = None
    encoding_format: Optional[str] = "float"

class ModelCard(BaseModel):
    id: str
    object: str = "model"
    created: int = 1724140800
    owned_by: str = "omnimind-ai"

class ModelList(BaseModel):
    object: str = "list"
    data: List[ModelCard]


class ResponseRequest(BaseModel):
    model: str
    input: Optional[Union[str, List[Any]]] = None
    instructions: Optional[str] = None
    previous_response_id: Optional[str] = None
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_output_tokens: Optional[int] = None
    stream: Optional[bool] = False
    reasoning: Optional[Dict[str, Any]] = None
    reasoning_effort: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    background: Optional[bool] = None
    parallel_tool_calls: Optional[bool] = None
    truncation: Optional[str] = None
    user: Optional[str] = None

    class Config:
        extra = "allow"
