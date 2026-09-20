#!/usr/bin/env python3
"""
DSH 中间层：拦截 OpenAI 兼容请求，走 DSR1 + Qwen 管道
"""
import json
import time
import uuid
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from diaoyongai import dsr1_classify, qwen_polish
from logger import log_session_start, log_dsr1, log_qwen, log_final, log_error
# ===== 配置 =====
LISTEN_PORT = 11435

app = FastAPI()


def extract_last_user_message(messages):
    """从 OpenAI 格式消息列表中提取最后一条 user 消息"""
    for m in reversed(messages):
        if m.get("role") == "user":
            return m.get("content", "")
    return ""


def build_response(text, model_name):
    """构造 OpenAI 兼容响应"""
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_name,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def stream_response(text, model_name):
    """把完整文本切成小块，模拟 SSE 流式输出"""
    resp_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
    created = int(time.time())

    # 第一帧：role
    first = {
        "id": resp_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model_name,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }
    yield f"data: {json.dumps(first, ensure_ascii=False)}\n\n"

    # 分片输出
    for ch in text:
        chunk = {
            "id": resp_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_name,
            "choices": [{"index": 0, "delta": {"content": ch}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

    # 结束帧
    end = {
        "id": resp_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model_name,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(end, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    stream = body.get("stream", False)
    model_name = body.get("model", "dsh-middleware")

    user_input = extract_last_user_message(messages)
    if not user_input:
        return JSONResponse(build_response("（没有识别到用户输入）", model_name))

    # 生成 session id（短哈希）
    session_id = uuid.uuid4().hex[:6]
    log_session_start(session_id, user_input[:200], model_name, stream, len(messages))

    # 跑管道
    try:
        intent = dsr1_classify(user_input, session_id)
        reply = qwen_polish(user_input, intent, history=messages, session_id=session_id)
    except Exception as e:
        log_error(session_id, "pipeline", e)
        reply = f"[管道错误] {e}"

    log_final(session_id, reply)

    if stream:
        return StreamingResponse(
            stream_response(reply, model_name),
            media_type="text/event-stream",
        )
    return JSONResponse(build_response(reply, model_name))



@app.get("/v1/models")
async def list_models():
    """DSH 有时会查模型列表，返回一个假的即可"""
    return {
        "object": "list",
        "data": [
            {"id": "dsh-middleware", "object": "model", "created": int(time.time()), "owned_by": "local"}
        ],
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=LISTEN_PORT)