#!/usr/bin/env python3
"""
模型调用层：封装所有对 Ollama 的调用
DSR1 意图分类 / Qwen 润色
"""
import json
import requests
from logger import log_session_start, log_dsr1, log_qwen, log_final, log_error, log_event

# ===== 配置 =====
OLLAMA_URL = "http://127.0.0.1:11434/v1/chat/completions"
DSR1_MODEL = "deepseek-r1:14b"
QWEN_MODEL = "R4C3R/qwen2.5-14b-instruct-heretic:q5_k_m"
TIMEOUT = 120
ASTRA_MODEL = "hf.co/mradermacher/ASTRA-14B-Thinking-v1-i1-GGUF:Q4_K_M"

def call_ollama(model, system_prompt, user_input, temperature=0.3):
    """调用 Ollama，返回文本"""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input},
        ],
        "temperature": temperature,
        "stream": False,
    }
    resp = requests.post(OLLAMA_URL, json=payload, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def dsr1_classify(user_input, session_id="unknown"):
    """DSR1 意图判断"""
    system_prompt = """你是意图分类器。根据用户输入，只输出一行 JSON，格式如下：
{"intent": "类别", "key_info": "关键信息摘要"}

类别只能是：CHAT / QUERY / TASK / REMINDER / RECORD

规则：
- CHAT：纯聊天、情感表达、开放性讨论
- QUERY：查询信息（天气、时间、资料）
- TASK：需要执行操作（创建文件、修改、部署）
- REMINDER：设置提醒
- RECORD：记录信息

只输出 JSON，不解释，不分析。"""
    raw = call_ollama(DSR1_MODEL, system_prompt, user_input, temperature=0.1)
    raw = raw.strip("`").replace("json\n", "").strip()
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        result = {"intent": "UNKNOWN", "key_info": raw}
    log_dsr1(session_id, raw, result)
    return result




def qwen_polish(user_input, intent_data, history=None, session_id="unknown"):
    """Qwen 润色：根据意图生成回复"""
    history_text = ""
    if history:
        filtered = [m for m in history if m.get("role") in ("user", "assistant")]
        filtered = filtered[:-1]
        if filtered:
            history_text = "\n最近对话：\n" + "\n".join(
                f"{m['role']}: {(m.get('content') or '')[:100]}" for m in filtered[-4:]
            )

    intent = intent_data.get("intent", "CHAT")

    # 按意图区分硬约束
    if intent in ("QUERY", "TASK", "REMINDER", "RECORD"):
        constraint = """- 你的回复只允许表达"我将会做某事"，禁止表达"我已经做完某事"
- 严禁使用"我查到了""我已经完成""已经帮您""已经记下"等表示已完成的措辞
- 只能表达"我这就去""马上帮您""准备执行"等未来时态
- 不要假装执行过任何操作"""
    else:
        constraint = "- 直接自然回应用户，保持口语化"

    system_prompt = f"""你是对话助手。用户输入经过意图分析：
意图：{intent}
关键信息：{intent_data.get('key_info', '')}
{history_text}

请根据用户原始输入，生成一段自然、简洁的回复。
规则：
- 不要提及"意图""分析"等技术词汇
- 保持口语化，像日常对话
{constraint}
- 回复控制在 80 字以内"""

    reply = call_ollama(QWEN_MODEL, system_prompt, user_input, temperature=0.7)
    log_qwen(session_id, reply)
    return reply


# 工具定义（ASTRA 规划时用，不执行）
TOOLS_SCHEMA = [
    {"type": "function", "function": {"name": "bash", "description": "执行 shell 命令", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "read_file", "description": "读取文件", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "写入文件", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "web_search", "description": "联网搜索", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "get_time", "description": "获取当前时间", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "set_reminder", "description": "设置提醒", "parameters": {"type": "object", "properties": {"time": {"type": "string"}, "content": {"type": "string"}}, "required": ["time", "content"]}}},
    {"type": "function", "function": {"name": "record_note", "description": "记录信息到笔记", "parameters": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}}},
]


def call_astra_tools(user_input, system_prompt, tools):
    """调用 ASTRA 并返回 tool_calls（原生协议）"""
    payload = {
        "model": ASTRA_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input},
        ],
        "tools": tools,
        "temperature": 0.3,
        "stream": False,
    }
    resp = requests.post(OLLAMA_URL, json=payload, timeout=TIMEOUT)
    resp.raise_for_status()
    msg = resp.json()["choices"][0]["message"]
    return msg.get("tool_calls", []), msg.get("content", "")


def astra_plan(user_input, intent_data, session_id="unknown"):
    """ASTRA 规划工具调用，但不执行"""
    system_prompt = f"""你是任务规划器。根据用户输入和意图，输出工具调用计划。

用户意图：{intent_data.get('intent')}
关键信息：{intent_data.get('key_info')}

规则：
- 只输出必须的工具调用，不要多此一举
- 如果用户输入不需要任何工具，就不要调用
- 不要解释，不要分析"""

    tool_calls, content = call_astra_tools(user_input, system_prompt, TOOLS_SCHEMA)
    log_event("astra_plan", input=user_input, intent=intent_data, tool_calls=tool_calls, content=content[:300])
    return tool_calls, content


