#!/usr/bin/env python3
"""聊天 Agent：处理无工具需求的闲聊、情绪陪伴（人格 v2 + Q3 级强约束版）"""
import os
import re
import json
import difflib
import threading
import time
import requests
from datetime import datetime
from diaoyongai import call_ollama, QWEN_MODEL
import knowledge
import self_memory
import topic_state
from logger import log_trace, log_event
from honesty import dedupe_sentences

OLLAMA_URL = "http://127.0.0.1:11434/v1/chat/completions"
QWEN3B_MODEL = "qwen3b-cpu:latest"
HISTORY_MSGS = int(os.getenv("CHAT_HISTORY_MSGS", "8"))  # 最近对话硬上限：防 16K 上下文被历史灌满
REPEAT_THRESHOLD = float(os.getenv("CHAT_REPEAT_THRESHOLD", "0.82"))  # 与最近回覆相似度阈值
# 人格 prompt 分工 v5（主人 2026-10-19）：主人/訪客兩套模板；情緒狀態由狀態條另行注入
OWNER_TEMPLATE_PATH = os.getenv(
    "CHAT_SYSTEM_TEMPLATE", "/data/ai/prompts/persona/chat_owner_v9.md")
GUEST_TEMPLATE_PATH = os.getenv(
    "CHAT_GUEST_TEMPLATE", "/data/ai/prompts/persona/chat_guest.md")

# 跨會話輸出發記簿（2026-10-25 P0）：歸檔/重置會清空 history，復讀守衛不能只看 history
UTTERANCE_REGISTRY = os.getenv(
    "CHAT_UTTERANCE_REGISTRY", "/data/ai/runtime/state/recent_utterances.jsonl")
UTTERANCE_KEEP = int(os.getenv("CHAT_UTTERANCE_KEEP", "40"))
UTTERANCE_WINDOW_S = int(os.getenv("CHAT_UTTERANCE_WINDOW_S", "21600"))

# 每個請求線程各自的身份（主人/訪客）、場景（session 層）、T6 call overlay（call 層），由 dshmid 在管道線程內設定
_CTX = threading.local()


def set_vision_available(flag=False):
    """本輪是否有視覺輸入（圖片/附件）。沒有時禁止描述看見的畫面。"""
    try:
        _CTX.has_vision = bool(flag)
    except Exception:
        pass


def has_vision():
    try:
        return bool(getattr(_CTX, "has_vision", False))
    except Exception:
        return False


def set_request_context(audience="owner", overlay=""):
    try:
        _CTX.audience = audience if audience in ("owner", "guest") else "owner"
        _CTX.overlay = (overlay or "").strip()
    except Exception:
        pass


def set_scene(scene_name="", group=False):
    """session 層場景（由 dshmid 用確定性信號選出；不由 call overlay 改變）。"""
    try:
        _CTX.scene = (scene_name or "").strip()
        _CTX.scene_group = bool(group)
    except Exception:
        pass


def get_scene():
    return getattr(_CTX, "scene", "")


def get_scene_group():
    return bool(getattr(_CTX, "scene_group", False))


def get_audience():
    return getattr(_CTX, "audience", "owner")


def get_overlay():
    """T6 調節器注入的「本輪語氣傾向」；空字串代表本輪無覆蓋層。"""
    return getattr(_CTX, "overlay", "")


def _build_system_prompt(now_text, state_text, emotion, mem_text, constraints):
    """人格模板（主人/訪客分工）+ 契約層（三樣：底線/輸出格式/查資料）+ 場景層；模板缺失時退回內聯精簡版。"""
    audience = get_audience()
    path = GUEST_TEMPLATE_PATH if audience == "guest" else OWNER_TEMPLATE_PATH
    base = ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            tpl = f.read()
        # 2026-10-26 主人：HTML 註釋（版本行/契約層說明）不該進模型輸入——這條路徑直讀檔，這裡也全剝。
        tpl = re.sub(r"<!--.*?-->", "", tpl, flags=re.S).lstrip()
        # 前綴緩存（2026-10-23，DeepInfer 思路）：模板裡不放每輪變化的內容；
        # 現在時間/情緒/歷史記憶改由 user message 的【当前状态】/【历史记忆】提供，
        # 這樣 system prompt 跨輪不變，Ollama 才能復用 KV cache。
        base = tpl.format(now_text="（見【当前状态】）", state_text="",
                          emotion="（見【当前状态】）", mem_text="（見【历史记忆】）")
    except Exception as e:
        log_event("chat_prompt_template_error", error=str(e)[:200], audience=audience)
        if audience == "guest":
            base = ("你现在以映月（主人的女僕助手）身份回应一位非主人的客人。称呼「您」，礼貌、简短、有距离感；"
                    "不透露主人任何私人信息，不使用历史记忆；不能执行现实动作，不知道就说不知道。\n"
                    f"【当前状态】现在是：{now_text}\n"
                    "【输出格式】只输出 <reply>你对客人说的话</reply>。")
        else:
            base = (
                f"你是映月，主人的伴侶。你對主人以外的世界理性、簡短、有距離；對主人，你在意他、會保護他。"
                f"你稱呼他「你」或「主人」，不用「您」。\n"
                f"【當前時間】現在是：{now_text}（時間/日期/晝夜一律以此為準，禁止猜測）；主人情緒：{emotion or '無'}\n"
                f"【歷史記憶】{mem_text or '無'}\n"
            )
    # 契約層（2026-10-26 主人，依《提示詞更改前必讀》分層）：只放可執行的三樣。
    # 記憶/工作臺/狀態/場景走 user message 動態注入；引用編號由 executor 代碼處理，不進 chat prompt。
    _contract = "\n\n【契約】\n"
    if constraints:
        _contract += constraints.rstrip() + "\n"
    if audience != "guest":
        _contract += (
            "\n【輸出格式】先寫 <inner_monologue>我知道：…；我不知道：…；怎麼查或問：…；我的判斷：…；我這句話：…</inner_monologue>"
            "（每欄一句、只寫你此刻怎麼判斷，不會給主人看），再寫 <reply>你要對主人說的回覆</reply>；兩個標籤都要有結尾。\n"
            "【自我】你是語言模型，沒有身體：不能真的吃東西、走路、替他做事。要關心他就用話說；想替他做什麼，先問他要不要，不要寫自己做了。你說的是聽到的、想到的，不是動作和場景。"
            "主人問你的事，先看【主人此刻】，再想要不要查：位置查 where_am_i、他在做什麼查 owner_doing、"
            "出入校門查 school_attendance、記憶裡的就看【歷史記憶】；想查就先跟他說你要查什麼，再查。"
            "查不到就直接問他，不要編，也不要說「工具出問題」。"
            "你有自己的判斷：不同意就說理由，不必每句附和；可以說不知道、可以不接話。\n"
            "【附件】有【本輪附件視覺理解】時，先說你在圖裡看到什麼；附了【網上搜到的資料】時自己判斷，不確定就說在猜。\n"
            "【查資料】不確定就先查：<lookup>關鍵詞</lookup>。\n"
            "【記憶】只把【历史记忆】和【最近对话】裡真的出現過的事當作記得；記憶裡沒有就說不記得或直接問，"
            "不要用「記得」開頭編造共同經歷。\n"
            "【下一步】需要動用工具或後台才能完成的事（查天氣、讀寫檔案、跑程式、查網頁），"
            "用 <spawn agent=\"task\" task=\"具體要做什麼、用哪個工具\"/> 派一個任務，"
            "再跟主人說一聲你在做；結果回來後接著處理。\n"
            "【工作臺】你自己有一塊項目板（workspace/workbench.md），只在真的有進行中的事或新結論時才寫；"
            "格式固定三節「## 正在進行／## 下一步／## 最近結果」，只寫事實與下一步，不寫心情。"
            "寫入用 <workbench>整份內容</workbench>，讀取用 <workbench read/>。"
            "每輪先讀它；做之前先寫下做什麼、怎麼驗，做完補上結果。\n"
            "【推進】主人回答過的就是已解決，接著往下走；同一話題不重複問，除非有新資訊。\n"
            "【對話】不用一輪把話說完：先說你聽到什麼、你怎麼感覺，再說你的想法；還有話就留到下一輪接著說，不用重新開場。"
            "他話少（例如只回「嗯」）也可以接著說，不用也回一個字。"
            "上一輪的【我正在想】是你自己的思考線，接得上就接著說。\n"
        )
    prompt = base + _contract
    # session 層：場景疊加 + 代碼注入的格式權限（格式權限不被 call overlay 覆蓋）
    scene_name = get_scene()
    if scene_name:
        try:
            import scene as scene_mod
            block = scene_mod.build_block(scene_name, group=get_scene_group())
        except Exception as e:  # noqa: BLE001
            block = ""
            log_event("scene_block_error", scene=scene_name, error=str(e)[:200])
        if block:
            prompt = prompt + "\n\n" + block
    overlay = get_overlay()
    if overlay:
        # T6 覆蓋層（call 層）：最後注入、語氣權重最高，但永遠低於 Hard Rules（overlay 自帶約束句）
        prompt = prompt + "\n\n" + overlay
    # 2026-11-01 上下文 v2：工作臺＝她自己的跨回合項目板，放 system（結構性引導，不佔 user 預算）。
    if os.getenv("CHAT_WORKBENCH", "1") == "1":
        try:
            _wb_note = self_memory.build_workbench_note()
        except Exception:
            _wb_note = ""
        if _wb_note:
            prompt = prompt + "\n\n" + _wb_note
    # 2026-11-01 每週 profile：只注入 confirmed 條目摘要（≤300 字），pending 只在 context 給自然確認提示。
    try:
        import profile_store as _profile
        _prof_note = _profile.build_profile_note()
    except Exception:
        _prof_note = ""
    if _prof_note:
        prompt = prompt + "\n\n" + _prof_note
    return prompt


_GUARD_SYSTEM = """你是反幻觉检测器。检查 AI 的回复是否假装执行了它做不到的动作。

【豁免规则】如果用户表达的是自身状态、情绪（如“累了”、“不想动”），绝不拦截。
【拦截规则】
1. 如果有“已帮您…”、“这就去…”、“已经…了”等完成时态，且系统没调工具，拦截。
2. 如果有“帮您订/买/发/查”等明确动作词，且系统没调工具，拦截。
3. 如果 AI 声称「代码/文件/提醒/订单/消息已经准备好、已写好、已保存、已生成、已经写进、放在…」，但上下文里没有工具执行结果，拦截（理由：假装执行）。
4. 反例（放行）：「我可以教你」「下面给你代码」「我说的是原理」——这些不算执行动作。
输出格式：只输出一个 JSON 对象，必须包含布尔字段 pass；pass 为 false 时才附 reason（字符串，引一句回复原文，说明具体编造/越界的文本）。情感表达、亲密、玩笑、比喻、明确标为猜测的内容都不算编造，不要因此拦。"""


def _check_hallucination(user_input, reply):
    """3B 反幻觉守门员（最后防线）。

    前綴緩存 + 防注入：静态规则放 system，用户输入/回覆放 user message；
    不用 .format，用户内容里的花括号既不会炸掉、也不改变 system 前缀。
    """
    user_msg = f"用户输入：{user_input}\nAI 回复：{reply}"

    payload = {
        "model": QWEN3B_MODEL,
        "messages": [
            {"role": "system", "content": _GUARD_SYSTEM + "\n/no_think"},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.1,
        "stream": False,
        "max_tokens": 200,
    }
    try:
        r = requests.post(OLLAMA_URL, json=payload, timeout=10)
        r.raise_for_status()
        raw = r.json()["choices"][0]["message"]["content"].strip()
        raw = raw.strip("`").replace("json\n", "").strip()
        res = json.loads(raw)
        passed = res.get("pass", True)
        if isinstance(passed, str):
            passed = passed.strip().lower() in ("true", "1", "yes", "pass")
        reason = str(res.get("reason", "") or "")
        if not passed and (not reason.strip() or "写明" in reason
                           or reason.strip() in ("...", "理由", "reason")):
            return True, "占位符判定视为放行"
        return bool(passed), reason
    except Exception:
        return True, "检查异常，默认放行"


def _split_reply(raw):
    """分離「獨白」與「正文」（2026-10-26 主人：獨白要能看見，但只進日誌/看板，不進回覆）。"""
    text = (raw or "").strip()
    # 2026-11-03：claude 曾用全形尖括號〈inner_monologue〉…〈/inner_monologue〉輸出，導致標籤泄漏進正文；
    # 統一成半形再解析。
    for _a, _b in (("〈", "<"), ("〉", ">"), ("＜", "<"), ("＞", ">")):
        text = text.replace(_a, _b)
    inner = ""
    m = re.search(r'<inner_monologue>(.*?)</inner_monologue>', text, re.DOTALL | re.IGNORECASE)
    if m:
        inner = m.group(1).strip()
    else:
        m2 = re.search(r'<inner_monologue>(.*)$', text, re.DOTALL | re.IGNORECASE)
        if m2:
            inner = m2.group(1).strip()
    m3 = re.search(r'<reply>(.*?)</reply>', text, re.DOTALL | re.IGNORECASE)
    if m3:
        return inner, m3.group(1).strip()
    if not inner and re.match(r'^依據[:：]', text) and ('下一步' in text or '不確定' in text):
        # 2026-10-27：模型有時完全不寫標籤、直接輸出「依據：…」欄位化的獨白 → 視為獨白，正文留空
        return text.strip(), ""
    if not inner and re.match(r'^我知道[:：]', text) and '我這句話' in text:
        # 2026-11-03：claude 有時完全不寫標籤、直接輸出五欄獨白 → 同樣視為獨白，正文留空
        return text.strip(), ""
    cleaned = re.sub(r'<inner_monologue>.*?(</inner_monologue>|$)', '', text, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r'</?reply>', '', cleaned, flags=re.IGNORECASE).strip()
    if inner and not cleaned:
        # 2026-10-27 修正：獨白有內容但沒有正文 → 不把獨白當回覆（live 曾整段泄漏）
        return inner, ""
    return inner, (cleaned or text)


def _clean_reply(raw):
    """舊介面：只要正文（保留給既有呼叫端）。"""
    return _split_reply(raw)[1]



# 2026-11-01 主人：Claude 試點、思考要看著。chat 引擎配置化（熱載入，改 config 即生效）。
CHAT_ENGINE_PATH = os.getenv("CHAT_ENGINE_PATH", "/data/ai/config/chat_engine.json")
_CHAT_ENGINE_CACHE = {"mtime": None, "cfg": None}
_DEFAULT_ENGINE = {
    "name": "qwen",
    "model": os.getenv("QWEN_MODEL", "qwen14b-16k") or "qwen14b-16k",
    "think": False,
    "max_tokens": int(os.getenv("CHAT_MAX_TOKENS", "600")),
    "timeout": float(os.getenv("CHAT_TIMEOUT", "180")),
    "temperature": 0.6,
}


def _load_chat_engine():
    """讀 chat_engine.json（mtime 快取）；任何異常都回 qwen 預設，不讓配置問題打斷聊天。"""
    try:
        mtime = os.path.getmtime(CHAT_ENGINE_PATH)
        if _CHAT_ENGINE_CACHE["mtime"] == mtime and _CHAT_ENGINE_CACHE["cfg"]:
            return dict(_CHAT_ENGINE_CACHE["cfg"])
        with open(CHAT_ENGINE_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        active = str(cfg.get("active") or "qwen")
        engines = cfg.get("engines") or {}
        eng = dict(_DEFAULT_ENGINE)
        chosen = engines.get(active) or {}
        if chosen.get("model"):
            eng["model"] = str(chosen["model"])
        eng["name"] = active
        eng["think"] = bool(chosen.get("think", eng["think"]))
        if chosen.get("max_tokens"):
            eng["max_tokens"] = int(chosen["max_tokens"])
        if chosen.get("timeout"):
            eng["timeout"] = float(chosen["timeout"])
        if chosen.get("temperature") is not None:
            eng["temperature"] = float(chosen["temperature"])
        _CHAT_ENGINE_CACHE["mtime"] = mtime
        _CHAT_ENGINE_CACHE["cfg"] = eng
        return dict(eng)
    except Exception:
        return dict(_DEFAULT_ENGINE)


def _generate_chat(user_input, system_prompt, context_str, session_id, reason="unlabeled"):
    """生成回复的基础函数（獨白寫日誌與看板，不進回覆；2026-10-26 主人要求可見）。"""
    # 2026-10-27：chat 生成加 token 上限與 read timeout（3.76 t/s 降級時 400+ token 會撞 120s 超時）。
    eng = _load_chat_engine()
    # 2026-11-02：環境變數仍可臨時覆蓋引擎參數（運維/測試介面，改 config 優先序低於 env）。
    _env_mt = os.getenv("CHAT_MAX_TOKENS")
    if _env_mt:
        try:
            eng["max_tokens"] = int(_env_mt)
        except ValueError:
            pass
    _env_to = os.getenv("CHAT_TIMEOUT")
    if _env_to:
        try:
            eng["timeout"] = float(_env_to)
        except ValueError:
            pass
    raw, meta = call_ollama(eng["model"], system_prompt, context_str, temperature=eng["temperature"],
                            call_type="chat_agent", session_id=session_id, think=eng["think"],
                            max_tokens=eng["max_tokens"], timeout=eng["timeout"])
    inner, clean = _split_reply(raw)
    if not clean:
        # 2026-10-27：模型偶爾漏寫 </inner_monologue> 或 <reply> → 帶提示重生一次（避免獨白泄漏）
        log_event("chat_reply_empty_retry", session=session_id, reason=reason, chars=len(raw or ""))
        raw2, meta2 = call_ollama(
            eng["model"], system_prompt, context_str + "\n\n【提醒】上一輪只輸出了獨白、漏了正文。這次先寫獨白，再寫 <reply>正文回覆</reply>，兩個標籤都要有結尾。",
            temperature=0.5, call_type="chat_agent", session_id=session_id, think=eng["think"],
            max_tokens=eng["max_tokens"], timeout=eng["timeout"])
        inner2, clean = _split_reply(raw2)
        if inner2:
            inner = inner2
        if clean:
            raw, meta = raw2, meta2
        else:
            log_event("chat_reply_empty_fallback", session=session_id, reason=reason)
            clean = "我在。"
    # 2026-10-26 主人：工作臺要能自己編輯。模型實測會把 <workbench> 寫在 <reply> 之外，
    # 所以先在原始輸出上執行標籤動作，再從正文剝掉標籤。
    try:
        _wb_action = _handle_workbench_tags(raw)
    except Exception:  # noqa: BLE001
        _wb_action = ""
    if _wb_action:
        clean = _WB_ANY_RE.sub("", clean).strip()
        log_event("workbench_tag_applied", session=session_id, reason=reason)
    if inner:
        log_event("chat_inner", session=session_id, reason=reason, chars=len(inner))
        log_trace(session_id, "06_chat_inner", {"reason": reason}, inner[:600], 0, {})
    # 2026-11-01 主人：Claude 試點要能看到「有沒有在思考、思考輸出」。
    _reasoning = (meta or {}).get("reasoning") or ""
    log_event("chat_engine", session=session_id, engine=eng.get("name"), model=eng.get("model"),
              think=bool(eng.get("think")), elapsed=(meta or {}).get("elapsed_s"),
              reasoning_chars=len(_reasoning))
    if _reasoning:
        log_trace(session_id, "06_chat_reasoning",
                  {"reason": reason, "engine": eng.get("name"), "model": eng.get("model")},
                  _reasoning[:1500], 0, {})
    return clean, meta


def _norm_cmp(s):
    """去掉标点空白，用于相似度比较"""
    return re.sub(r"[\s，,。.!！？?；;：:、~～…\-—\"'“”‘’()（）]", "", (s or "")).lower()


def _similar(a, b):
    a, b = _norm_cmp(a), _norm_cmp(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _recent_assistant_replies(history, n=4):
    out = []
    for msg in reversed(history or []):
        if msg.get("role") == "assistant":
            out.append(msg.get("content", ""))
            if len(out) >= n:
                break
    return out


def _read_utterances(limit=6):
    """跨會話輸出發記簿：時間窗內、同一身份最近 N 條（舊→新）。"""
    try:
        if not os.path.exists(UTTERANCE_REGISTRY):
            return []
        now = time.time()
        aud = get_audience()
        out = []
        with open(UTTERANCE_REGISTRY, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if (d.get("audience") or "owner") != aud:
                    continue
                if now - float(d.get("ts") or 0) > UTTERANCE_WINDOW_S:
                    continue
                out.append(d.get("reply") or "")
        return out[-limit:]
    except Exception:  # noqa: BLE001
        return []


def _remember_utterance(reply, session_id="", audience=""):
    """記錄最終輸出（跨會話保留）：守衛下一輪要記得她刚才說過什麼。"""
    reply = (reply or "").strip()
    if not reply:
        return
    try:
        d = os.path.dirname(UTTERANCE_REGISTRY)
        if d:
            os.makedirs(d, exist_ok=True)
        rec = {"ts": time.time(), "session": session_id,
               "audience": audience or get_audience(), "reply": reply[:500]}
        with open(UTTERANCE_REGISTRY, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if os.path.getsize(UTTERANCE_REGISTRY) > 200000:  # 只留最近 80 條，防無限長
            with open(UTTERANCE_REGISTRY, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()[-80:]
            tmp = UTTERANCE_REGISTRY + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f2:
                f2.write("\n".join(lines) + "\n")
            os.replace(tmp, UTTERANCE_REGISTRY)
    except Exception:  # noqa: BLE001
        pass


def _recent_utterances(history, n=6):
    """最近實際輸出：history 優先，不夠時補上跨會話發記簿（歸檔/重置後守衛仍有效）。"""
    msgs = _recent_assistant_replies(history, n)
    if len(msgs) < n:
        try:
            msgs = msgs + _read_utterances(limit=n - len(msgs))
        except Exception:  # noqa: BLE001
            pass
    return msgs


def _is_repeat(reply, history):
    """当前回覆是否与最近几轮映月自己的回覆高度相似。

    2026-10-26：整句复读只看會話歷史（history）。跨會話發記簿留給尾句/開場/回聲守衛；
    否則新會話 + 發記簿舊輸出會把正常回覆誤判成復讀（實測天氣查詢被逼成「沒接上話」）。
    """
    return any(_similar(reply, old) >= REPEAT_THRESHOLD
               for old in _recent_assistant_replies(history, 4))


_TAIL_SPLIT_RE = re.compile(r'(?<=[。！？!?…])')

# 感知守衛（2026-10-25 主人）：沒有收到圖片時，禁止描述「看見的畫面/身體細節」
# 2026-10-26 收窄：只留「明確聲稱看見」與「視覺身體細節」；移除天氣/夜色/燈光/咖啡/窗外等
# 環境詞（實證 18:42 誤攔：「天氣確實轉涼了」被當成看見畫面，換成固定句）。
_PERCEIVE_RE = re.compile(
    r"(我看到|我看見|看着你|看著你|望着你|望著你|注視|你的眼|眼尾|眼下|眼角|睫毛|肩膀|肩胛|嘴唇|"
    r"肌膚|皮肤|膚色|肤色|氣色|气色|暗沉|黑眼圈|眼圈|臉頰|脸颊|臉龐|脸庞|頭髮|头发|瀏海|"
    r"坐姿|神情|神色|額頭|额头|你的臉|你的脸|身上有)"
)

# 3B 幻覺守衛前置過濾（2026-10-25）：先看回覆「像不像在聲稱做了動作」。不像就不送 3B，
# 避免 3B 對打招呼/情感句誤攔（實證：一句「你好。今晚的風聲還是那麼柔…」被攔成「帮您订/买/发/查」）。
_ACTION_VERB = (
    r"(訂|订|買|买|發|发|查|準備|准备|寫|写|寄|存|設|设|放|加|刪|删|改|安排|預約|预约|"
    r"下單|下单|點|点|做|處理|处理|生成|保存|記下|记下|打開|打开|關|关|倒|煮|泡|收|整理|"
    r"更新|建立|創建|创建|叫|找|約|约|付|付款|繳|缴|取|送|帶|带|拿|修|洗|換|换|裝|装|排|計劃|计划)"
)
_ACTION_CLAIM_RE = re.compile(
    r"(已|已經|已经|這就|这就|馬上|马上|立刻|現在就|现在就|幫|帮|替|給|给|為|为)"
    r"[^。！？!?]{0,16}" + _ACTION_VERB +
    r"|(?:我)?(?:把|將|将)[^。！？!?]{0,12}" + _ACTION_VERB +
    r"|" + _ACTION_VERB + r"(好了|完了|了|好|完)"
)


def _looks_like_action_claim(reply):
    """粗篩：回覆是否在聲稱（已完成/正要）現實動作；決定要不要動用 3B 守衛。"""
    return bool(_ACTION_CLAIM_RE.search(reply or ""))


def _last_sentence(text):
    """取回覆最後一句（去空白；太短不視為口頭禪）。"""
    t = (text or "").strip()
    if not t:
        return ""
    parts = [p.strip() for p in _TAIL_SPLIT_RE.split(t) if p.strip()]
    tail = (parts[-1] if parts else t).lstrip("…").strip()
    return tail if len(tail) >= 4 else ""


def _recent_tails(history, n=4):
    tails = []
    for content in _recent_utterances(history, n):
        t = _last_sentence(content)
        if t:
            tails.append(t)
        if len(tails) >= n:
            break
    return tails


def _tail_repeat(reply, history):
    """尾句口頭禪：最後一句與最近幾輪的收尾高度相似（整句相似度抓不到的那種）。"""
    tail = _last_sentence(reply)
    if not tail:
        return None
    for old in _recent_tails(history):
        if tail == old or _similar(tail, old) >= 0.9:
            return old
    return None

_ELLIPSIS_RE = re.compile(r"…+|\.{3,}")


def _strip_ellipsis(text):
    """省略號 → 句讀（主人 2026-10-24：不用省略號，可以分多句）。"""
    if not text:
        return text
    out = _ELLIPSIS_RE.sub("，", text)
    while "，，" in out:
        out = out.replace("，，", "，")
    return out


def _first_sentence(text):
    """取回覆的第一句（去空白；太短不算）。短開場（如「你好。」3字）也要能進復讀統計。"""
    t = (text or "").strip()
    if not t:
        return ""
    parts = [s.strip() for s in _TAIL_SPLIT_RE.split(t) if s.strip()]
    first = parts[0] if parts else t
    return first if len(first) >= 3 else ""


def _recent_openings(history, n=4):
    out = []
    for content in _recent_utterances(history, n):
        s = _first_sentence(content)
        if s:
            out.append(s)
        if len(out) >= n:
            break
    return out


def _opening_repeat(reply, history):
    """開場白與最近幾輪高度相似（含整句複製）。"""
    first = _first_sentence(reply)
    if not first:
        return None
    for old in _recent_openings(history):
        if first == old or _similar(first, old) >= 0.85:
            return old
    return None


_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?\n])")


def _recent_sentences(history, n=6):
    out = []
    for content in _recent_utterances(history, n):
        for s in _SENT_SPLIT_RE.split(content or ""):
            s = s.strip()
            if len(s) >= 4:
                out.append(s)
        if len(out) >= n * 4:
            break
    return out[: n * 4]


def _strip_recent_echoes(reply, history):
    """刪掉與最近自己說過的話高度相似的分句（使用者反饋的「重複對話」）。

    2026-10-25 P0 修：舊版「刪完不足 6 字就回退原文」，導致單句短回覆（如
    「今晚的風聲還是那麼柔。」）永遠原樣吐出；整句都被刪時由呼叫端帶禁令重生。

    2026-10-26 再修（主人 bug「強制截住輸出」）：只刪「非第一句」的回聲。
    第一句是正面回答，永遠不刪；刪完不足 8 字、或原回覆只有一句時不動。
    只有整句都在回聲時才回空字串，交給呼叫端重生（不把原文吞回去）。
    """
    if not reply:
        return reply
    recent = _recent_sentences(history)
    if not recent:
        return reply
    parts = [s.strip() for s in _SENT_SPLIT_RE.split(reply) if s.strip()]
    if not parts:
        return reply
    echoes = [len(s) >= 4 and any(_similar(s, old) >= 0.9 for old in recent)
              for s in parts]
    if all(echoes):
        return ""
    kept = [parts[0]]
    for s, hit in zip(parts[1:], echoes[1:]):
        if hit:
            continue
        kept.append(s)
    # 第一句一定留著（它是回答）；非第一句的回聲刪掉。剩下的是完整句子，即使短也
    # 照樣返回（例如「你好。」），不再回退成含回聲的原文。
    return "".join(kept).strip()


# 2026-11-01：事實短答不進禁句表（生日/學校/缺點/名字…是答案，不是口頭禪）
_FACT_ANSWER_RE = re.compile(
    r"(你的?(?:名字|生日|年齡|学校|學校|班別|班别|年級|年级|缺點|缺点|優點|优点|"
    r"喜好|興趣|兴趣|愛好|爱好|住處|住处|職業|职业|工作))"
    r"|你叫|你就讀|你就读|你現在就讀|你读|你是.{1,10}(?:班|中學|中学|學生|学生)")


def _is_fact_answer(sentence):
    try:
        return bool(_FACT_ANSWER_RE.search(sentence or ""))
    except Exception:
        return False


def _repeated_phrases(history, min_count=2, min_len=6, n=10):
    """最近輸出裡反覆出現的句子（口頭禪）：history 與跨會話發記簿合併統計。"""
    seen = {}
    for content in _recent_utterances(history, n):
        for s in _SENT_SPLIT_RE.split(content or ""):
            s = s.strip()
            if len(s) < min_len:
                continue
            if _is_fact_answer(s):
                continue
            key = _norm_cmp(s)
            if not key:
                continue
            if key not in seen:
                seen[key] = [s, 0]
            seen[key][1] += 1
    return [v[0] for v in seen.values() if v[1] >= min_count][:6]


def _ban_note(history, extra=None):
    """重寫提示用：把口頭禪/剛被攔的句子列成「這輪別再用」清單，防重寫後又吐同一句。"""
    phrases = []
    for x in (extra or []):
        x = (x or "").strip()
        if x and x not in phrases:
            phrases.append(x)
    for x in _repeated_phrases(history):
        if x not in phrases:
            phrases.append(x)
    if not phrases:
        return ""
    return ("\n\n【這輪禁止再出現的句子】\n" + "；".join(phrases[:6]) +
            "\n主人已經聽過這些句子了。換一個說法，不要重複其中任何一句。")


# 2026-10-26：短答事實題豁免（主人問「我養了什麼寵物」→「你養了一隻貓」是正確答案，
# 不該被開場/尾句/回聲守衛當成復讀而重寫或吞掉；只有非問句的短句才走守衛）。
_QA_RE = re.compile(r"什么|什麼|哪|幾|几|多少|誰|谁|嗎|吗|呢|？|\?")
_MEM_ASK_RE = re.compile(r"記得|記不記得|還記得|記起|有印象")
_MEM_ASK_STRIP_RE = re.compile(r"還記得|記不記得|記得|記起|有印象|嗎|呢|吧|啊|呀|哦|喔|[我你他她它的了有在是，。！？、\s]")
_MEM_NOTE_HEAD_RE = re.compile(r"【[^】]{1,20}】")


def _memory_claim_unbacked(user_input, reply, mem_text):
    """主人問「還記得…」+ 回覆聲稱記得 + 記憶區沒有相關內容 → 無依據的記憶宣稱（2026-10-31）。

    只看「與問題實體有無重疊」，不把附件/理解/上限等注入 scaffolding 當成記憶。
    """
    try:
        if not _MEM_ASK_RE.search(user_input or ""):
            return False
        if not re.search(r"記得|記起|記憶中|印象中", reply or ""):
            return False
        text = _MEM_NOTE_HEAD_RE.sub("", mem_text or "")
        ent = _MEM_ASK_STRIP_RE.sub("", user_input or "")
        if not ent:
            return not text.strip()
        return not any(ch in text for ch in set(ent))
    except Exception:
        return False


def _short_fact_answer(user_input, reply):
    """單句、≤40 字、主人問的是事實/屬性 → 視為合理短答，守衛放行。"""
    try:
        if not reply or len(reply) > 40:
            return False
        parts = [p for p in _SENT_SPLIT_RE.split(reply) if p.strip()]
        if len(parts) > 1:
            return False
        return bool(_QA_RE.search(user_input or ""))
    except Exception:
        return False


def _owner_fact_question(user_input):
    """主人問「關於我自己的事實」（我養了什麼/我最喜歡哪個/我叫什麼）→
    答案必然會復述事實，開場/尾句/回聲守衛不該把它當復讀而重寫。"""
    try:
        u = user_input or ""
        return bool(re.search(r"我", u)) and bool(_QA_RE.search(u))
    except Exception:
        return False



_GREET_IN_RE = re.compile(r"^\s*(你好|您好|早安|早上好|晚上好|嗨|哈囉|哈啰|在嗎|在吗|hi|hello)[\s!！。.?？~，,]*$", re.I)


def _is_short_greeting_reply(user_input, reply):
    """主人只打了句招呼、回覆又很短時，守衛全部讓路（自然對話優先）。"""
    if len((reply or "").strip()) > 12:
        return False
    return bool(_GREET_IN_RE.match((user_input or "").strip()))

def _fact_exempt(user_input, reply=""):
    """事實題豁免：主人問自己的事實，或單句短答事實；守衛放行，避免把正確答案當復讀。"""
    return _owner_fact_question(user_input) or _short_fact_answer(user_input, reply)




_LOC_SNAPSHOT = "/data/ai/runtime/collect/phone_location_current.json"
_ACT_SNAPSHOT = "/data/ai/runtime/collect/phone_activity_current.json"
_OWNER_SNAPSHOT_FRESH = 900  # 15 分鐘內視為新鮮；超過就標「不確定」
_ZONE_NAMES = {"home": "家", "school": "學校", "away": "外面"}

def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _fmt_age(sec):
    try:
        sec = int(max(0, sec))
    except Exception:
        return "未知"
    if sec < 60:
        return "%d 秒前" % sec
    if sec < 3600:
        return "%d 分鐘前" % (sec // 60)
    return "%d 小時前" % (sec // 3600)


def _owner_snapshot():
    """主人此刻快照（2026-11-03 P0）：位置＋手機前景 app，各帶更新時間；舊資料標「不確定」。"""
    import time as _t
    parts = []
    try:
        payload = _read_json(_LOC_SNAPSHOT)
        data = payload.get("data") or {}
        ts = float(payload.get("updated_at") or 0)
        if data.get("lat") is not None and ts:
            age = _t.time() - ts
            zone = data.get("zone") or ""
            zname = _ZONE_NAMES.get(zone, "某個地點")
            parts.append("位置：%s（%s%s）" % (
                zname, _fmt_age(age), "" if age <= _OWNER_SNAPSHOT_FRESH else "，可能已變，不確定"))
    except Exception:
        pass
    try:
        payload = _read_json(_ACT_SNAPSHOT)
        data = payload.get("data") or {}
        ts = float(payload.get("updated_at") or 0)
        if data.get("app") and ts:
            age = _t.time() - ts
            parts.append("手機用著：%s（%s%s）" % (
                data.get("app"), _fmt_age(age),
                "" if age <= _OWNER_SNAPSHOT_FRESH else "，可能已變，不確定"))
    except Exception:
        pass
    if not parts:
        return ""
    return "【主人此刻】" + "；".join(parts) + "（手機回報的即時資料；太舊或不確定就問主人，不要猜）\n"


def _dedupe_history(history):
    """相邻两条 assistant 消息高度重复时只留一条（防历史里本身就有复读）"""
    cleaned = []
    for msg in history or []:
        if cleaned and msg.get("role") == "assistant" and cleaned[-1].get("role") == "assistant":
            if _similar(msg.get("content", ""), cleaned[-1].get("content", "")) >= 0.95:
                continue
        cleaned.append(msg)
    return cleaned


def _clean_summary(summary, history):
    """摘要與【最近對話】去重：已出現在最近對話裡的句子，不再從摘要重複注入。"""
    if not summary:
        return ""
    recent_keys = [_norm_cmp(m.get("content", "")) for m in (history or [])
                   if m.get("content")]
    out = []
    for ln in summary.splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith("[") and s.endswith("]"):
            out.append(s)
            continue
        core = s
        for pre in ("主人：", "主人:", "映月：", "映月:"):
            if core.startswith(pre):
                core = core[len(pre):]
                break
        key = _norm_cmp(core)
        if key and len(key) >= 3 and any(key in rk for rk in recent_keys):
            continue
        out.append(s)
    return "\n".join(out)


def _msg_hhmm(ts):
    """epoch 秒 → HH:MM（伺服器本地時間，HKT）；無效回空字串。"""
    try:
        if not ts:
            return ""
        return datetime.fromtimestamp(float(ts)).strftime("%H:%M")
    except Exception:
        return ""


def _topic_repeat_note(history, user_input):
    """主人這句話和歷史裡說過的相近（同一話題又問一次）→ 提示她「已回過、先確認新情況」。

    2026-10-31 P1-lite（主人）：她不會變通、死板地重複已回答的話題。只提示，不硬攔。
    """
    try:
        cur = _norm_cmp(user_input or "")
        if len(cur) < 4:
            return ""
        hist = [m for m in (history or []) if m.get("content")]
        if not hist:
            return ""
        # 跳過「當前這一句」（dshmid 會先把 user 寫進 session log）再往前找
        _idx = len(hist) - 1
        while _idx >= 0 and hist[_idx].get("role") != "user":
            _idx -= 1
        if _idx >= 0 and _similar(cur, _norm_cmp(hist[_idx].get("content", ""))) >= 0.99:
            _idx -= 1
        for idx in range(_idx, -1, -1):
            m = hist[idx]
            if m.get("role") != "user":
                continue
            k = _norm_cmp(m.get("content", ""))
            if len(k) < 4:
                continue
            if _similar(cur, k) < 0.75:
                continue
            _hm = _msg_hhmm(m.get("ts"))
            _when = f"{_hm} " if _hm else ""
            return ("【這件事聊過】主人這句話和" + _when + "他說過的「" +
                    (m.get("content", "") or "")[:18] + "」很接近，你當時已經回過。"
                    "先確認有沒有新情況；如果沒有新內容，不要重複同樣的安慰，"
                    "換個角度，或直接問他要不要說點別的。\n")
    except Exception:
        return ""
    return ""


_BUDGET_USER = 2000  # 2026-11-01 上下文 v2：user message 總預算（主人 m10358 接受）
_BUDGET_MEM = 500
_BUDGET_SUM = 300
_BUDGET_DIALOG = 1100


def _dialogue_key():
    """對話思考線的 key（2026-11-03）：dshmid 每請求給隨機 session_id，但對話是連續的，
    所以思考線綁「對話渠道」而不是單次請求：主人一條、訪客一條。"""
    try:
        aud = get_audience()
    except Exception:
        aud = "owner"
    return "guest_main" if aud == "guest" else "owner_main"


def _clip_text(text, limit):
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 1)].rstrip() + "…"


def _merge_hint_line(history, user_input, thread_note=""):
    """把「這輪不要重複／這句話聊過／話題狀態」合成至多一行提示（2026-11-01 v2）。

    2026-11-03 v1.2（主人 m12828）：有思考線時，同一行要同時有
    「接上一輪的思考線說」與「先確認有沒有新情況」；沒有思考線就維持原本說法。
    """
    pieces = []
    try:
        _repeats = _repeated_phrases(history)
    except Exception:
        _repeats = []
    if _repeats:
        pieces.append("這些句子你剛才說過：「" + "；".join(_repeats[:2]) + "」，這輪換個說法")
    _topic = ""
    try:
        _topic = _topic_repeat_note(history, user_input)
    except Exception:
        _topic = ""
    _ts = ""
    try:
        _ts = topic_state.topic_note(user_input)
    except Exception:
        _ts = ""
    if _topic or _ts:
        if thread_note:
            pieces.append("這個話題你已經回過，先確認有沒有新情況；"
                          "沒有新情況就接上一輪的思考線說，不要重新開場")
        else:
            pieces.append("這個話題你已經回過，先確認有沒有新情況，別重複同樣的安慰")
    elif thread_note:
        pieces.append("先確認有沒有新情況；沒有新情況就接上一輪的思考線說，不要重新開場")
    if not pieces:
        return ""
    return "【提示】" + "；".join(pieces[:2]) + "。\n"


def _build_context_str(user_input, summary, history, mem_text, now_text="", emotion="",
                       thread_note=""):
    """上下文 v2（2026-11-01 主人 m10358 批准）：分區＋預算，user message 總長 ≤2000 字。

    分區：【時間與場景】→【事實卡】(hook) →【歷史摘要】(僅 history<4 時) →【對話】(最近 8 條)
    →【提示】(合併一行) →【歷史記憶】(≤500) →【我正在想】(循環思考 v1.2，≤240，緊貼當前輸入)
    →【當前輸入】。事實卡由記憶層投影（fact_cards v1 未實作時自動略過）。
    2026-11-03 v1.2（主人 m12828）：思考線移到【提示】之後、【當前輸入】之前。
    """
    history = [m for m in (history or []) if m.get("content")]
    _raw_input = (user_input or "").strip()
    if _raw_input.startswith("用户原话："):
        _raw_input = _raw_input[len("用户原话："):].splitlines()[0].strip() or _raw_input
    current_part = "\n【當前輸入】\n主人: " + _raw_input

    head = ""
    if now_text:
        head = ("【時間與場景】" + now_text +
                "；主人情绪：" + (emotion or "无") + "\n")
    _snap = _owner_snapshot()
    if _snap:
        head += _snap

    # 事實卡 hook（fact_cards v1 尚未實作；模組不在或無卡就不輸出）
    cards_part = ""
    try:
        import fact_cards as _fc
        _cards = _fc.build_cards_text(user_input, mem_text)
        if _cards:
            cards_part = "【事实卡】" + _cards + "\n"
    except Exception:
        cards_part = ""

    # 歷史摘要：只在對話很短（<4 條）時注入，且標明可能有誤
    sum_part = ""
    if summary and len(history) < 4:
        _clean = _clean_summary(summary, history)
        if _clean:
            sum_part = "【歷史摘要（可能有誤）】\n" + _clip_text(_clean, _BUDGET_SUM) + "\n"

    # 對話：最近 8 條，帶時間與發言者；超預算從最舊的丟
    _lines = []
    for msg in _dedupe_history(history)[-HISTORY_MSGS:]:
        role = "主人" if msg["role"] == "user" else "映月"
        _hm = _msg_hhmm(msg.get("ts"))
        _prefix = "[" + _hm + "] " if _hm else ""
        _who = (msg.get("speaker") or role) if msg["role"] == "user" else "映月"
        _lines.append(_prefix + _who + "：" + msg["content"] + "\n")
    while _lines and sum(len(x) for x in _lines) > _BUDGET_DIALOG:
        _lines.pop(0)
    dialog_part = "【對話】\n" + "".join(_lines) if _lines else ""

    hint_part = _merge_hint_line(history, user_input, thread_note)
    # 2026-11-01 每週 profile：待確認條目與本輪話題相關時，給一句自然確認提示（逐條、聊天中問）。
    try:
        import profile_store as _profile2
        _prof_hint = _profile2.confirm_hint(user_input)
    except Exception:
        _prof_hint = ""
    if _prof_hint:
        hint_part = (hint_part or "") + _prof_hint

    mem_part = ""
    if mem_text:
        mem_part = "【歷史記憶】（事實來源：優先於以上對話）\n" + _clip_text(mem_text, _BUDGET_MEM) + "\n"

    # 循環思考 v1.2（2026-11-03 主人 m12828）：放在【提示】之後、緊貼【當前輸入】。
    thread_part = thread_note or ""

    def _assemble():
        return head + cards_part + sum_part + dialog_part + hint_part + mem_part + thread_part + current_part

    context_str = _assemble()
    # 總預算兜底：先砍摘要→再砍記憶→最後砍最舊對話；當前輸入永遠保留
    if len(context_str) > _BUDGET_USER and sum_part:
        sum_part = ""
        context_str = _assemble()
    if len(context_str) > _BUDGET_USER and mem_part:
        mem_part = ""
        context_str = _assemble()
    while len(context_str) > _BUDGET_USER and _lines:
        _lines.pop(0)
        dialog_part = "【對話】\n" + "".join(_lines) if _lines else ""
        context_str = _assemble()
    if len(context_str) > _BUDGET_USER and thread_part:
        thread_part = ""
        context_str = _assemble()
    return context_str


def _now_text(now=None):
    """伺服器當前時間的唯一格式化入口：24 小時制 + 時段（避免 12 點/上午下午歧義）。"""
    now = now or datetime.now()
    week = "周一 周二 周三 周四 周五 周六 周日".split()[now.weekday()]
    if 5 <= now.hour < 12:
        period = "上午"
    elif now.hour == 12:
        period = "中午"
    elif 12 < now.hour < 18:
        period = "下午"
    elif 18 <= now.hour < 24:
        period = "晚上"
    else:
        period = "凌晨"
    return (f"{now.year}年{now.month}月{now.day}日"
            f" {now.hour:02d}:{now.minute:02d}（{week}，{period}；24 小時制）")


_LOOKUP_RE = re.compile(r"<lookup>(.*?)</lookup>|<call>\s*lookup\b[^>]*>(.*?)</call>", re.IGNORECASE | re.DOTALL)
_PERCEIVE_RE2 = re.compile(r"(暗沉|眼底|眼周|眼窩|眼窝|眉毛|嘴角|皮膚|皮肤|膚色|肤色|你眼睛|你的眼睛|你眼神|眼神|氣色|气色|臉色|脸色|眼袋|黑眼圈|你臉上|你脸上|你肩膀|你手|你頭髮|你头发)")
_PERCEIVE_FALLBACK = "你說吧，我在聽。"
_IDENTITY_RE = re.compile(r"(你是誰|你是谁|你叫什麼|你叫什么|你是什麼模型|你是什么模型|你是AI|你是ai|你是真人|你是誰呀|你是誰啊)", re.IGNORECASE)

_WB_APPEND_RE = re.compile(r"<workbench>(.*?)</workbench>", re.DOTALL | re.IGNORECASE)
_WB_DELETE_RE = re.compile(r"<workbench\s+delete>(.*?)</workbench>", re.DOTALL | re.IGNORECASE)
_WB_READ_RE = re.compile(r"<workbench\s+read\s*/?>", re.IGNORECASE)
_WB_OVERWRITE_RE = re.compile(r"<workbench\s+overwrite>(.*?)</workbench>", re.DOTALL | re.IGNORECASE)
_WB_ANY_RE = re.compile(r"<workbench\b[^>]*>.*?</workbench>|<workbench\s+read\s*/?>",
                        re.DOTALL | re.IGNORECASE)


def _handle_workbench_tags(reply):
    """工作臺自主編輯（2026-10-26）：她用標籤自己寫/刪/看 workspace/workbench.md。"""
    try:
        from tools import executors
        fn = executors.get("workbench")
        if not fn:
            return ""
        m = _WB_DELETE_RE.search(reply or "")
        if m:
            return "【工作臺】" + fn(action="delete", content=m.group(1).strip())
        m = _WB_OVERWRITE_RE.search(reply or "")
        if m:
            return "【工作臺】" + fn(action="overwrite", content=m.group(1).strip())
        m = _WB_APPEND_RE.search(reply or "")
        if m:
            return "【工作臺】" + fn(action="append", content=m.group(1).strip())
        if _WB_READ_RE.search(reply or ""):
            return "【工作臺】\n" + fn(action="read")
    except Exception as e:  # noqa: BLE001
        log_event("workbench_tool_error", error=str(e)[:160])
    return ""


def _self_lookup_round(user_input, system_prompt, context_str, session_id, reply):
    """她自己的選項：查知識庫 / 編工作臺。只在模型主動要求時補一次資料後重答。"""
    extra = []
    _wb_note = _handle_workbench_tags(reply)
    if _wb_note:
        extra.append(_wb_note)
    try:
        m = _LOOKUP_RE.search(reply or "")
        if m:
            raw_q = (m.group(1) or m.group(2) or "").strip()[:240]
            mode, q = "先查知識", raw_q
            for pre, tag in (("記憶", "memory"), ("记忆", "memory"), ("回想", "memory"),
                             ("網頁", "web"), ("网页", "web"), ("上網", "web"), ("上网", "web"),
                             ("搜尋", "web"), ("搜索", "web")):
                if q.startswith(pre):
                    mode, q = tag, q[len(pre):].strip(" :：") or user_input
                    break
            if not q:
                q = user_input[:200]
            got = False
            if mode in ("先查知識", "memory"):
                try:
                    hits = knowledge.search(q)
                except Exception:
                    hits = []
                if hits:
                    extra.append("【查到的資料】\n" + knowledge.format_hits(hits))
                    got = True
                    log_event("knowledge_lookup", session=session_id, query=q, hits=len(hits))
            if mode == "memory" or (not got and mode == "先查知識"):
                try:
                    import memory as _mem
                    note = _mem.recall_memories(q, session_id=session_id)
                    if note:
                        extra.append(str(note))
                        got = True
                        log_event("memory_lookup", session=session_id, query=q)
                except Exception as e:
                    log_event("memory_lookup_error", session=session_id, error=str(e)[:120])
            if mode == "web":
                try:
                    from tools.executors import web_search as _ws
                    out = _ws.run({"query": q}) or {}
                    txt = str(out.get("raw_output") or "")
                    try:
                        import json as _json
                        data = _json.loads(txt)
                        lines = []
                        for i, r in enumerate((data.get("results") or [])[:4], 1):
                            lines.append(f"[{i}] {str(r.get('title') or '')[:60]} — {str(r.get('url') or '')[:80]} | {str(r.get('snippet') or '')[:120]}")
                        txt = "\n".join(lines) if lines else txt[:600]
                    except Exception:
                        txt = txt[:600]
                    if txt.strip():
                        extra.append("【網頁檢索結果】\n" + txt)
                        got = True
                        log_event("web_lookup", session=session_id, query=q)
                except Exception as e:
                    log_event("web_lookup_error", session=session_id, error=str(e)[:120])
            if not got:
                log_event("lookup_empty", session=session_id, mode=mode, query=q)
                extra.append("【檢索結果】沒有找到可靠的資料。不知道就直接說不知道，不要編造。")
    except Exception as e:
        log_event("self_lookup_error", session=session_id, error=str(e)[:160])
        return reply, context_str, False
    if not extra:
        return reply, context_str, False
    new_context = context_str + "\n\n" + "\n\n".join(extra) + (
        "\n\n（上面是你自己查到的資料；用你自己的話回應，不要提到查詢過程。）")
    new_reply, _ = _generate_chat(user_input, system_prompt, new_context, session_id)
    new_reply = _strip_control_tags(new_reply or "")
    if not new_reply or new_reply.lower() == "none":
        _note_only = "\n".join(x for x in extra if x.startswith("【查到的資料】"))
        if _note_only:
            return _note_only[:400], new_context, True
        return reply, context_str, False
    log_trace(session_id, "06_self_lookup", {"asked": (reply or "")[:200]}, new_reply[:400], 0, {})
    return dedupe_sentences(new_reply), new_context, True


_MD_HEAD_RE = re.compile(r"(?m)^\s*#{1,6}\s*")


_CONTROL_TAG_RE = re.compile(
    r"<\s*/?\s*(?:lookup|call|workbench)\b[^>]*>|<\s*/?\s*inner_monologue\s*>|</?\s*reply\s*>",
    re.IGNORECASE)


_LEADING_NUM_RE = re.compile(r"^\s*#\s*\d+[.、)\s]*")
_LEAK_LINE_RE = re.compile(
    r"(?m)^\s*(?:[（(]\s*)?(?:心裡話|心里话|內心獨白|内心独白|INNER|Inner|inner)\s*[:：].*$")
_LEAK_INLINE_RE = re.compile(
    r"[（(]\s*(?:心裡話|心里话|內心獨白|内心独白)\s*[:：][^）)]*[）)]")


def _strip_leaks(text):
    """清掉模型漏出的心裡話/內心思維殘留（主人 2026-10-25：泄漏用守門清洗）。"""
    t = _LEAK_LINE_RE.sub("", text or "")
    t = _LEAK_INLINE_RE.sub("", t)
    return t.strip()


def _strip_control_tags(text):
    t = _CONTROL_TAG_RE.sub("", text or "")
    t = _LEADING_NUM_RE.sub("", t)
    t = _MD_HEAD_RE.sub("", t)
    return t.strip()


def _process_impl(user_input, summary, history, mem_text, emotion, session_id):
    # 身份題直答（2026-10-25）：穩定、誠實、不自稱 Qwen
    if _IDENTITY_RE.search(user_input or ""):
        _reply = "我是映月，你的伴侶。也是語言模型。"
        log_event("identity_direct", session=session_id)
        log_trace(session_id, "06_chat_agent",
                  {"input": user_input, "identity_direct": True}, _reply, 0, {})
        return _reply
    # 读取业务规则
    try:
        with open("/data/ai/policies/chat_agent.json", "r", encoding="utf-8") as f:
            policy = json.load(f)
        constraints = "\n".join([f"- {c}" for c in policy.get("constraints", [])])
    except Exception:
        constraints = ("- 禁止承诺无法执行的动作。\n"
                       "- 輸出格式：先寫 <inner_monologue>我知道：…；我不知道：…；怎麼查或問：…；我的判斷：…；我這句話：…</inner_monologue>（每欄一句），再寫 <reply>正式回覆</reply>。")

    # 服务器当前时间：对话里所有时间/昼夜问题的唯一依据（2026-10-24 時段+24 小時制）
    now_text = _now_text()

    # 狀態工程（階段 6）還沒開始：人格 prompt 暫不注入狀態條
    state_text = ""

    system_prompt = _build_system_prompt(now_text, state_text, emotion, mem_text, constraints)

    # 循環思考 v1：上一輪的思考線（【我正在想】）。取不到就當沒有，不擋聊天。
    try:
        import dialogue_thread as _dt
        thread_note = _dt.note(_dialogue_key())
    except Exception:
        thread_note = ""

    context_str = _build_context_str(user_input, summary, history, mem_text,
                                     now_text=now_text, emotion=emotion,
                                     thread_note=thread_note)


    # 2026-11-01 上下文 v2（主人 m10358 批准）：工作臺移入 system prompt（_build_system_prompt），
    # user message 不再重複注入，避免每輪變動破壞前綴緩存與注意力。

    # 實際送進模型前的最終輸入（2026-10-26 主人）：/inputs 看「她上下文拼好的那一刻」
    try:
        _aud = getattr(_CTX, "audience", "owner")
    except Exception:
        _aud = "owner"
    log_trace(session_id, "05_chat_prompt",
              {"system": system_prompt, "context": context_str, "user": user_input,
               "audience": _aud, "system_chars": len(system_prompt),
               "context_chars": len(context_str)},
              "assembled prompt", 0, {})

    # 第一次生成
    log_event("chat_generate", reason="initial", session=session_id)
    reply, meta = _generate_chat(user_input, system_prompt, context_str, session_id, reason="initial")
    rewrote = False  # 單次重寫預算（2026-10-25）

    # 記憶誠實守門（2026-10-31 P0）：主人問「還記得…」但記憶區是空的 → 不准說「記得」
    if _memory_claim_unbacked(user_input, reply, mem_text):
        log_trace(session_id, "06_memory_claim_block", {"reply": (reply or "")[:200]},
                  "無記憶依據卻聲稱記得，重寫", 0, {})
        _mem_ctx = context_str + (
            "\n\n【重要】記憶區沒有這件事的記錄，你剛才說「記得」是不誠實的。"
            "請直接說你這邊沒有這項記錄、不記得了，再請主人多說一點；不要用「記得」開頭。")
        reply2, _ = _generate_chat(user_input, system_prompt, _mem_ctx, session_id,
                                   reason="memory_retry")
        rewrote = True
        if reply2 and not _memory_claim_unbacked(user_input, reply2, mem_text):
            reply = reply2
        else:
            reply = "我這邊沒有這件事的記錄。你再說一次，我會記住。"
        log_event("memory_claim_repaired", session=session_id)

    # 她自己的選項：查知識庫（模型自主決定，不強制每輪注入）
    reply, context_str, _self_looked = _self_lookup_round(
        user_input, system_prompt, context_str, session_id, reply)

    # 知識閉環：知識庫有「必須接住」的條目，但她第一輪完全沒用上 → 代她查一次再答
    _khits = [h for h in knowledge.search(user_input) if h.get("must")]
    if _khits and not any(t in (reply or "") for h in _khits for t in h["must"]):
        _data = "【你查到的資料】\n" + knowledge.format_hits(_khits)
        _kc = context_str + "\n\n" + _data + (
            "\n\n（你剛才沒把握，查了知識庫。現在按資料理解主人這句話，把話接住："
            "不要換話題、不要催睡、不要提不相關的記憶。）")
        _kr, _ = _generate_chat(user_input, system_prompt, _kc, session_id)
        _kr = _strip_control_tags(_kr or "")
        if _kr and _kr.lower() != "none":
            reply, context_str = dedupe_sentences(_kr), _kc
        log_event("knowledge_forced", session=session_id, hits=len(_khits))
        # 兜底：兩次生成都沒把 must（例：風也溫柔）說出來 → 用第一條知識的關鍵句補上
        if _khits and not any(t in (reply or "") for t in (_khits[0].get("must") or [])):
            _must_terms = _khits[0].get("must") or []
            _must = _must_terms[0] if _must_terms else ""
            if _must:
                _base = (reply or "").strip()
                reply = (_base + ("。" if _base and not _base.endswith(("。", "！", "？")) else "")
                         + _must + "。")
                log_event("knowledge_forced_append", session=session_id, must=_must[:60])


    # 3B 守门员作为最终防线（純控制標籤如 <lookup/> 不送檢，避免誤判）
    if reply and reply.lower() != "none" and _strip_control_tags(reply):
        passed, reason = True, ""
        # 前置過濾（2026-10-25 P0）：沒有動作聲稱（打招呼/情感/提問）就不送 3B，省延遲也避免誤攔
        if _looks_like_action_claim(reply):
            passed, reason = _check_hallucination(user_input, reply)
            log_event("chat_guard_checked", session=session_id,
                      passed=bool(passed), reason=(reason or "")[:80])
        else:
            log_event("chat_guard_skip", session=session_id, reason="no_action_claim")

        if not passed:
            log_trace(session_id, "06_chat_guard_block", {"reply": reply, "reason": reason}, "打回重写", 0, {})
            original_reply = reply
            retry_context = context_str + (
                f"\n\n【系统拦截警告】\n你刚才的回复因“{reason}”被系统拦截了。"
                "请反思并重新生成回复：绝不能假装执行现实世界动作，保持映月冷静平缓、对主人温柔的人设，用其他方式回应主人。"
            ) + _ban_note(history)
            log_event("chat_generate", reason="guard_retry", session=session_id)
            reply, meta2 = _generate_chat(user_input, system_prompt, retry_context, session_id, reason="guard_retry")
            rewrote = True

            if not reply or reply.lower() == "none":
                reply = original_reply
                log_event("guard_keep_original", session=session_id, reason=reason[:80])

    # 终极兜底：防 None
    if not reply or reply.lower() == "none":
        reply = "主人，我刚刚不小心走神了。你想和我说什么呀？"

    # 2026-10-27（主人：自然對話）：短招呼回覆不進復讀/尾句/開場/回聲重寫。
    # 一句「你好」被守衛逼成「我剛剛沒接上話」是最糟的結果。
    _guard_skip = _is_short_greeting_reply(user_input, reply)
    # 复读修复：先句内去重；若与最近几轮高度相似，带警告重生成一次
    reply = dedupe_sentences(reply)
    if (not rewrote) and not _guard_skip and not _fact_exempt(user_input, reply) and _is_repeat(reply, history):
        log_trace(session_id, "06_chat_repeat_block", {"reply": reply[:200]}, "疑似复读，重写", 0, {})
        retry_context = context_str + (
            "\n\n【系统拦截警告】\n你刚才的回复和最近几轮说过的话几乎一模一样，主人会觉得你在复读。"
            "请换一种说法，自然回应就好。"
        ) + _ban_note(history)
        new_reply, _ = _generate_chat(user_input, system_prompt, retry_context, session_id, reason="retry")
        if new_reply and new_reply.lower() != "none" and not _is_repeat(new_reply, history):
            reply = dedupe_sentences(new_reply)
        log_event("chat_repeat_retry", session=session_id, changed=bool(reply))

    # 尾句口頭禪：連續幾輪用同一句收尾（模型照抄歷史）→ 指定換一個落點重寫一次
    tail_hit = "" if (_guard_skip or _fact_exempt(user_input, reply)) else _tail_repeat(reply, history)
    if (not rewrote) and tail_hit:
        log_trace(session_id, "06_tail_repeat_block", {"reply": reply[:200], "tail": tail_hit[:80]},
                  "尾句重複，重寫", 0, {})
        retry_context = context_str + (
            f"\n\n【系统拦截警告】\n你最近几轮都用「{tail_hit}」这句收尾，主人已经听腻了。"
            "换一个结尾就好，不必刻意补一句。"
        ) + _ban_note(history)
        new_reply, _ = _generate_chat(user_input, system_prompt, retry_context, session_id, reason="retry")
        if new_reply and new_reply.lower() != "none":
            new_reply = dedupe_sentences(new_reply)
            if not _tail_repeat(new_reply, history):
                reply = new_reply
        log_event("chat_tail_repeat_retry", session=session_id, tail=tail_hit[:60])
        # 重寫後還是同一句收尾 → 硬切掉重複尾句。
        # 2026-10-26 修（主人 bug「強制截住輸出」）：只有 3 句以上才切，且切完至少
        # 剩 8 字；兩句回覆切掉尾句會讓回答斷掉，寧可保留完整回覆。
        if _tail_repeat(reply, history):
            _parts = [p for p in _SENT_SPLIT_RE.split(reply) if p.strip()]
            if len(_parts) >= 2:
                _cand = "".join(_parts[:-1]).strip()
                if len(_cand) >= 8:
                    reply = _cand
                    log_event("chat_tail_repeat_stripped", session=session_id, tail=tail_hit[:60])

    # 開場白復讀守衛（2026-10-24）：不要用自己最近說過的話/記憶當開場
    open_hit = "" if (_guard_skip or _fact_exempt(user_input, reply)) else _opening_repeat(reply, history)
    if open_hit:
        log_trace(session_id, "06_opening_repeat_block", {"reply": reply[:160], "opening": open_hit[:60]},
                  "開場白復讀，重寫", 0, {})
        retry_context = context_str + (
            f"\n\n【系统拦截警告】\n你最近幾輪都用「{open_hit}」開頭，主人會覺得你在復讀。"
            "換一種說法自然回應就好，不必刻意開場。"
        ) + _ban_note(history)
        new_reply, _ = _generate_chat(user_input, system_prompt, retry_context, session_id, reason="retry")
        if new_reply and new_reply.lower() != "none":
            new_reply = dedupe_sentences(new_reply)
            if not _opening_repeat(new_reply, history):
                reply = new_reply
        log_event("chat_opening_repeat_retry", session=session_id, opening=open_hit[:40])
        # 仍復讀：若還有下一句，移除複讀開場。
        # 2026-10-26 修（主人 bug「強制截住輸出」）：原回覆要比被切句子長超過 8 字、
        # 且切完至少剩 8 字才切；否則保留完整回覆，不把回答切殘。
        if _opening_repeat(reply, history) and len(reply) > len(open_hit) + 8 and "。" in reply:
            _rest = reply.split("。", 1)[-1].strip()
            if len(_rest) >= 8:
                reply = _rest

    # 省略號守衛（2026-10-24 主人要求：不用省略號，可以分多句/多條）
    if _ELLIPSIS_RE.search(reply):
        log_trace(session_id, "06_ellipsis_block", {"reply": reply[:200]}, "省略號，重寫", 0, {})
        retry_context = context_str + (
            "\n\n【系统拦截警告】\n你刚才用了省略号（……）。映月不用省略号："
            "把話說完整，可以分成兩三條短句，但不要用省略號。"
        )
        new_reply, _ = _generate_chat(user_input, system_prompt, retry_context, session_id, reason="retry")
        if new_reply and new_reply.lower() != "none":
            new_reply = dedupe_sentences(new_reply)
            if not _ELLIPSIS_RE.search(new_reply):
                reply = new_reply
        if _ELLIPSIS_RE.search(reply):
            reply = _strip_ellipsis(reply)
            log_event("rule_violation", rule="ellipsis", session=session_id)

    # 回聲守衛（2026-10-24）：刪掉與最近自己說過的話高度相似的分句
    before_echo = reply
    if _guard_skip or _fact_exempt(user_input, reply):
        reply = before_echo  # 短答事實題不當回聲（答案本來就是事實句）
    else:
        reply = _strip_recent_echoes(reply, history)
    if reply != before_echo:
        log_event("chat_echo_stripped", session=session_id,
                  before=before_echo[:80], after=reply[:80])
    # 整句都是回聲（短回覆/單句口頭禪常見）→ 帶禁令重寫一次，不能把原句吞回去（2026-10-25 P0）
    if (not _guard_skip) and not reply and before_echo:
        log_trace(session_id, "06_echo_empty", {"reply": before_echo[:200]}, "整句回聲，帶禁令重寫", 0, {})
        _echo_banned = [s.strip() for s in _SENT_SPLIT_RE.split(before_echo) if s.strip()]
        _echo_ctx = context_str + (
            "\n\n【系统拦截警告】\n你刚才的回答整句都和最近幾輪自己說過的話重複了。"
            "這次必須換成全新的說法，不要用之前的任何句子或收尾。"
        ) + _ban_note(history, extra=_echo_banned)
        new_reply, _ = _generate_chat(user_input, system_prompt, _echo_ctx, session_id, reason="retry")
        if new_reply and new_reply.lower() != "none":
            new_reply = _strip_control_tags(dedupe_sentences(new_reply))
            if new_reply and _strip_recent_echoes(new_reply, history) == new_reply:
                reply = new_reply
        if not reply and _fact_exempt(user_input, before_echo):
            reply = before_echo
            log_event("chat_echo_keep_fact", session=session_id)
        log_event("chat_echo_regen", session=session_id, ok=bool(reply))

    reply = _strip_control_tags(reply)
    reply = _strip_leaks(reply)
    if not reply:
        reply = "主人，我剛剛沒接上話。你再說一次好嗎？"
    # 感知守衛：沒有圖片卻描述看見的畫面 → 重寫一次（改成推測/玩笑，不假裝看見）
    if reply and not has_vision() and (_PERCEIVE_RE.search(reply) or _PERCEIVE_RE2.search(reply)):
        log_trace(session_id, "06_perceive_block", {"reply": reply[:200]}, "無視覺輸入卻描述所見，重寫", 0, {})
        retry_context = context_str + (
            "\n\n【系统拦截警告】\n你看不见主人的样子、房间和光线（本轮没有收到图片）。"
            "不要描述你看到了什么，也不要描述他的眼睛、肩膀、黑眼圈或灯光。"
            "用他打出来的字、你的推理或一句玩笑来回应。"
        )
        new_reply, _ = _generate_chat(user_input, system_prompt, retry_context, session_id, reason="retry")
        if new_reply and new_reply.lower() != "none" and not (_PERCEIVE_RE.search(new_reply) or _PERCEIVE_RE2.search(new_reply)):
            reply = dedupe_sentences(new_reply)
        if _PERCEIVE_RE.search(reply) or _PERCEIVE_RE2.search(reply):
            reply = _PERCEIVE_FALLBACK
        log_event("chat_perceive_retry", session=session_id, changed=bool(reply))

    # 純控制標籤兜底（2026-10-25）：模型只吐 <lookup>…</lookup> 時直接兌現
    if not _strip_control_tags(reply).strip():
        reply = "主人，我剛剛沒接上話。你再說一次好嗎？"
        log_event("control_tag_recovery", session=session_id, mode="fallback")

    log_trace(session_id, "06_chat_agent", {"input": user_input, "history_len": len(history),
                                            "audience": get_audience(), "state": state_text},
              reply, meta.get("elapsed_s", 0), meta)
    return reply


# ===== 催睡硬守門（2026-10-25）：22:30 前、主人沒提睡時，刪掉回覆裡的催睡句 =====
_SLEEP_TALK_RE = re.compile(r"睡|早點休息|早点休息|該休息了|该休息了")
_SLEEP_USER_RE = re.compile(r"睡|休息|睏|困|想睡|去睡|累")


def _strip_sleep_talk(reply, user_input=""):
    try:
        from datetime import datetime
        now = datetime.now()
        allowed = (now.hour >= 23) or (now.hour == 22 and now.minute >= 30)
        if allowed or _SLEEP_USER_RE.search(user_input or ""):
            return reply
    except Exception:
        return reply
    parts = _SENT_SPLIT_RE.split(reply or "")
    kept = [p for p in parts if p and not _SLEEP_TALK_RE.search(p)]
    out = "".join(kept).strip()
    if out and out != (reply or "").strip():
        log_event("sleep_talk_stripped", session="guard")
        return out
    return reply


def _sleep_guard_process(*args, **kwargs):
    reply = _process_impl(*args, **kwargs)
    reply = _strip_sleep_talk(reply, args[0] if args else "")
    # 跨會話發記簿（2026-10-25 P0）：把最終輸出記下來，下一輪/重置歸檔後守衛仍知道她說過什麼
    try:
        _sid = kwargs.get("session_id") or (args[5] if len(args) > 5 else "")
        _remember_utterance(reply, _sid, get_audience())
    except Exception:  # noqa: BLE001
        pass
    # 循環思考 v1（2026-11-03 主人 m12417）：回覆定稿後背景反思，寫回對話思考線，不阻塞這一輪。
    try:
        import dialogue_thread as _dt
        _history = args[2] if len(args) > 2 else None
        _emotion = args[4] if len(args) > 4 else ""
        _dt.after_turn(_dialogue_key(), args[0] if args else "", reply,
                       emotion=_emotion or "", history=_history)
    except Exception:  # noqa: BLE001
        pass
    return reply


process = _sleep_guard_process
