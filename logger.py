#!/usr/bin/env python3
"""统一日志模块：人类可读的步骤流水账格式"""
import os
from datetime import datetime

LOG_DIR = "/data/ai/agents/logs"
_session_start = {}  # 记录每个 session 的开始时间


def _ensure_dir():
    os.makedirs(LOG_DIR, exist_ok=True)


def _log_path():
    date = datetime.now().strftime("%Y-%m-%d")
    return os.path.join(LOG_DIR, f"middleware-{date}.log")


def _write(lines):
    _ensure_dir()
    with open(_log_path(), "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _ts():
    return datetime.now().strftime("%H:%M:%S")


def log_session_start(session_id, user_input, model, stream, message_count):
    _session_start[session_id] = datetime.now()
    _write([
        "",
        f"========== [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Session: {session_id} ==========",
        f"[步骤 1/4] DSH 请求",
        f"  模型: {model} | 流式: {'是' if stream else '否'} | 消息数: {message_count}",
        f"  用户输入: {user_input}",
    ])


def log_dsr1(session_id, raw, parsed):
    _write([
        f"[步骤 2/4] DSR1 意图判断  ({_ts()})",
        f"  原始输出: {raw[:200]}",
        f"  解析结果: intent={parsed.get('intent')} | key_info={parsed.get('key_info')}",
    ])


def log_qwen(session_id, output):
    _write([
        f"[步骤 3/4] Qwen 润色  ({_ts()})",
        f"  输出: {output[:300]}",
    ])


def log_final(session_id, reply):
    start = _session_start.pop(session_id, None)
    elapsed = ""
    if start:
        elapsed = f"{(datetime.now() - start).total_seconds():.1f}s"
    _write([
        f"[步骤 4/4] 返回 DSH  ({_ts()})",
        f"  耗时: {elapsed}",
        f"  最终回复: {reply[:300]}",
        f"========== Session {session_id} 结束 ==========",
    ])


def log_error(session_id, stage, error):
    _write([
        f"[错误] Session {session_id} | 阶段: {stage}",
        f"  {str(error)}",
    ])