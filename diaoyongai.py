#!/usr/bin/env python3
"""
模型调用层：封装所有对 Ollama 的调用
DSR1 意图分类 / Qwen 润色
"""
import json
import requests
from logger import log_session_start, log_dsr1, log_qwen, log_final, log_error
# ===== 配置 =====
OLLAMA_URL = "http://127.0.0.1:11434/v1/chat/completions"
DSR1_MODEL = "deepseek-r1:14b"
QWEN_MODEL = "R4C3R/qwen2.5-14b-instruct-heretic:q5_k_m"
TIMEOUT = 120


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

    system_prompt = f"""你是对话助手。用户输入经过意图分析，结果如下：
意图：{intent_data['intent']}
关键信息：{intent_data['key_info']}
{history_text}

请根据用户原始输入，生成一段自然、简洁的回复。
规则：
- 不要提及"意图""分析"等技术词汇
- 保持口语化，像日常对话
- 如果是 CHAT，直接回应情绪或话题
- 如果是 QUERY，表示会去查询
- 如果是 TASK，确认任务并说明会执行
- 如果是 REMINDER 或 RECORD，确认已记下
- 回复控制在 80 字以内
- 如果意图是 QUERY 或 TASK，你只能说"我这就去查/去做"，禁止说"我查到了""我完成了"等表示已完成的措辞。因为执行是由后续工具完成的，你只负责表达层。
"""

    reply = call_ollama(QWEN_MODEL, system_prompt, user_input, temperature=0.7)
    log_qwen(session_id, reply)
    return reply
