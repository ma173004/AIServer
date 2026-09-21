#!/usr/bin/env python3
"""
DSH 中间层：拦截 OpenAI 兼容请求，走 DSR1 + Qwen 管道
"""
import json
import time
import uuid
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from diaoyongai import dsr1_classify, qwen_polish, astra_plan
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

def format_tool_plan(tool_calls, content=""):
    """把 tool_calls 格式化成人类可读的待处理计划"""
    if not tool_calls:
        return content or "（无计划）"

    lines = ["计划执行以下操作（待确认）："]
    for i, call in enumerate(tool_calls, 1):
        fn = call.get("function", {})
        name = fn.get("name", "?")
        args_raw = fn.get("arguments", "{}")
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
            args_str = ", ".join(f"{k}={v}" for k, v in args.items())
        except Exception:
            args_str = str(args_raw)

        if name == "bash":
            lines.append(f"  {i}. 执行命令: `{args.get('command', '')}`")
        elif name == "write_file":
            lines.append(f"  {i}. 写入文件: `{args.get('path', '')}`")
        elif name == "read_file":
            lines.append(f"  {i}. 读取文件: `{args.get('path', '')}`")
        elif name == "web_search":
            lines.append(f"  {i}. 联网搜索: `{args.get('query', '')}`")
        elif name == "get_time":
            lines.append(f"  {i}. 获取当前时间")
        else:
            lines.append(f"  {i}. {name}({args_str})")

    lines.append("")
    lines.append("回复「确认」执行，回复「取消」放弃。")
    return "\n".join(lines)


def save_pending(session_id, user_input, tool_calls):
    """把待处理计划存到文件"""
    import os
    pending_dir = "/data/ai/agent-state/pending"
    os.makedirs(pending_dir, exist_ok=True)
    path = f"{pending_dir}/{session_id}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "session_id": session_id,
            "user_input": user_input,
            "tool_calls": tool_calls,
            "created_at": time.time(),
            "status": "pending",
        }, f, ensure_ascii=False, indent=2)



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

    session_id = uuid.uuid4().hex[:6]
    log_session_start(session_id, user_input[:200], model_name, stream, len(messages))

    try:
        intent = dsr1_classify(user_input, session_id)
        intent_type = intent.get("intent", "CHAT")

        if intent_type == "CHAT":
            # 纯聊天走 Qwen
            reply = qwen_polish(user_input, intent, history=messages, session_id=session_id)
        else:
            # QUERY / TASK / REMINDER / RECORD 全部走 ASTRA
            tool_calls, content = astra_plan(user_input, intent, session_id)
            if tool_calls:
                reply = format_tool_plan(tool_calls, content)
                save_pending(session_id, user_input, tool_calls)
            else:
                # ASTRA 没规划出工具，如实告知
                reply = f"（ASTRA 未规划出可执行工具）\n意图：{intent_type}\n关键信息：{intent.get('key_info', '')}"
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
            {"id": "dshmid", "object": "model", "created": int(time.time()), "owned_by": "local"},
        ],
    }

@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=LISTEN_PORT)