"""
LINE Bot Dispatcher (Elior)
- elior（不分大小寫）→ Gemini 3.1 Flash Lite 回應
- 分析XX → 股票五維分析 (analyze_one.py)
- 每日股市推薦 → pre_market_report (main.py --dry-run)
- #XX → Gemini + Google Search 查即時資訊
"""

from flask import Flask, request, abort
import requests
import os
import subprocess
import re
import json
import subprocess
import threading
import logging

app = Flask(__name__)
app.logger.setLevel(logging.INFO)
# 新增一個 log handler 到檔案，方便 Elior 讀取
file_handler = logging.FileHandler("/home/eeyore/line_message.log")
file_handler.setLevel(logging.INFO)
app.logger.addHandler(file_handler)


# === Elior LINE Bot 憑證 ===
LINE_CHANNEL_SECRET = os.environ.get(
    "LINE_CHANNEL_SECRET",
    "49545400cf26d079f63e11e66118e061"
)
LINE_ACCESS_TOKEN = os.environ.get(
    "LINE_ACCESS_TOKEN",
    "4oLvvt065m7SOAWOVqw2LPTbUZk/XiGQnO7zatE0GZbEgms/e5BPD8eO9p1Z8doit8BG7xtz37T1oBggkz8rmbmB/5ZUhwWChb/0PvFjXHn7q3S0jrBbNox1TFVr0OyukpH9fTkQ/1yfR4ayPEitjAdB04t89/1O/w1cDnyilFU="
)

# 生日快樂影片 URL（精確匹配「生日快樂」時發送）
BIRTHDAY_VIDEO_URL = "https://raw.githubusercontent.com/eowilling/line-bot-assets/main/birthday.mp4"
BIRTHDAY_VIDEO_THUMBNAIL = "https://raw.githubusercontent.com/eowilling/line-bot-assets/main/birthday_thumbnail.jpg"

# === Gemini API ===
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AIzaSyA97Mhy4KU_YZ7pI1u9fP9fyY88TOmi17I")

# === Maton API Gateway（Google Workspace）===
MATON_API_KEY = os.environ.get(
    "MATON_API_KEY",
    "5cZHTIIoZ1l2KUyHi3nTvcnYcgqblrfUKLmYVyD6PZIc2TzMXKNLutRmz2SC48zdZoSi0gkZH-szhrPia6yvaTEQzvAnTBQ3pew"
)
MATON_BASE = "https://gateway.maton.ai"
MATON_HEADERS = {"Authorization": f"Bearer {MATON_API_KEY}", "Content-Type": "application/json"}
GOOGLE_CALENDAR_ID = "eo.willing@gmail.com"
GOOGLE_TASKS_SHOPPING_LIST_ID = "ZFNoUG1XYms5VkI5RmtRRA"
GOOGLE_TASKS_DEFAULT_LIST_ID = "MDc1NzUwNDI5MTg3NTQ4NDU1ODY6MDow"
GOOGLE_DOC_LINKS_ID = "1h8XfITyUX-JWGNNGSRBPWxlx0jzvGmI-4Y2FpyXTxmQ"

# === 路徑 ===
PROJECT_DIR = "/home/eeyore/openclaw/pre_market_report"
VENV_PY = os.path.join(PROJECT_DIR, "pre_market_report_venv", "bin", "python3")

# LINE 名稱 → 稱呼 對照表
NICKNAME_MAP = {
    "哲輝": "爸爸",
    "KaiChen": "小可爹地",
    "eo": "帥哥哥",
    "KU": "小貓姨姨",
    "Yi-Ting Chiang": "暴龍姨姨",
    "ERIC": "胖嘟嘟叔叔",
    "HANA": "椛椛姨姨",
    "Yvonne": "三星上校",
}

display_name_cache = {}


def get_display_name(user_id, group_id=None):
    """LINE API 查名稱 → 稱呼表比對"""
    cache_key = f"{group_id}:{user_id}" if group_id else user_id
    if cache_key in display_name_cache:
        return display_name_cache[cache_key]
    line_name = None
    try:
        if group_id:
            url = f"https://api.line.me/v2/bot/group/{group_id}/member/{user_id}"
        else:
            url = f"https://api.line.me/v2/bot/profile/{user_id}"
        resp = requests.get(url, headers={"Authorization": f"Bearer {LINE_ACCESS_TOKEN}"}, timeout=5)
        if resp.status_code == 200:
            line_name = resp.json().get("displayName")
    except Exception:
        pass
    # 完全匹配或前綴匹配（LINE 名稱可能帶後綴 emoji/文字）
    matched = None
    if line_name:
        if line_name in NICKNAME_MAP:
            matched = NICKNAME_MAP[line_name]
        else:
            for key, nick in NICKNAME_MAP.items():
                if line_name.startswith(key):
                    matched = nick
                    break
    if matched:
        result = matched
    else:
        result = line_name or "某人"
    display_name_cache[cache_key] = result
    return result


# ========================
# LINE 訊息發送
# ========================

def line_reply(reply_token, text):
    """用 replyToken 回覆"""
    msgs = [{"type": "text", "text": text[i:i+4500]} for i in range(0, len(text), 4500)]
    try:
        r = requests.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={"Authorization": f"Bearer {LINE_ACCESS_TOKEN}", "Content-Type": "application/json"},
            json={"replyToken": reply_token, "messages": msgs[:5]},
            timeout=10
        )
        if r.status_code != 200:
            app.logger.error(f"line_reply 失敗 ({r.status_code}): {r.text[:200]}")
    except Exception as e:
        app.logger.error(f"line_reply 異常: {e}")


def line_push(target, text):
    """用 push 主動發送"""
    msgs = [{"type": "text", "text": text[i:i+4500]} for i in range(0, len(text), 4500)]
    for msg in msgs:
        requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": f"Bearer {LINE_ACCESS_TOKEN}", "Content-Type": "application/json"},
            json={"to": target, "messages": [msg]},
            timeout=10
        )


# ========================
# Elior（Gemini 2.5 Flash）
# ========================

ELIOR_MODEL = "gemini-2.5-flash"
_youtube_search_cache = {} # 通用 YouTube 搜尋快取 {("關鍵字", "日期"): ["影片1", "影片2", ...]}
_zodiac_cache = {}  # 星座每日快取 {「水瓶座_2026-04-21」: 回應文字}

def _handle_youtube_search(target, reply_token, sender_name, keywords, title_prefix, title_max_len=60):
    """通用 YouTube 搜尋與推薦函數"""
    from datetime import datetime
    import re
    import random
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        cache_key = (keywords, today)
        
        if cache_key not in _youtube_search_cache:
            _youtube_search_cache[cache_key] = []
        recommended_today = _youtube_search_cache[cache_key]
        
        app.logger.info(f"{title_prefix}觸發！今日已推薦: {len(recommended_today)} 首")
        
        searxng_url = "http://localhost:8888/search"
        params = {
            'q': f'{keywords} official MV site:youtube.com',
            'categories': 'general',
            'format': 'json',
            'pageno': 1,
            'language': 'en'
        }
        
        import requests
        resp = requests.get(searxng_url, params=params, timeout=10, verify=False)
        
        if resp.status_code != 200:
            app.logger.error(f"SearxNG 搜尋失敗: {resp.status_code}")
            line_reply(reply_token, f"🎵 {title_prefix}推薦系統暫時無法使用，請稍後再試～")
            return
        
        data = resp.json()
        results = data.get('results', [])
        
        if not results:
            app.logger.error("SearxNG 無搜尋結果")
            line_reply(reply_token, f"🎵 {title_prefix}推薦系統暫時無法使用，請稍後再試～")
            return
        
        app.logger.info(f"SearxNG 找到 {len(results)} 個結果")
        
        candidates = []
        for result in results:
            title = result.get('title', '')
            url = result.get('url', '')
            
            if keywords.lower() in title.lower() and ('youtu.be' in url or 'youtube.com/watch' in url):
                if '/channel/' in url or '/user/' in url or '/playlist' in url or '/@' in url:
                    continue
                
                clean_title = title.strip()
                if len(clean_title) > 50:
                    match = re.search(rf'{keywords}[^-]*-\s*([^(\\[|]+)', clean_title, re.IGNORECASE)
                    if match:
                        song_name = match.group(1).strip()
                        clean_title = f"{keywords} - {song_name}"
                
                candidates.append((clean_title, url))
        
        if not candidates:
            app.logger.error(f"無法從 SearxNG 結果中提取 {keywords} MV")
            line_reply(reply_token, f"🎵 {title_prefix}推薦系統暫時無法使用，請稍後再試～")
            return
        
        app.logger.info(f"找到 {len(candidates)} 首 {keywords} 歌曲")
        
        recommended_urls = [url for _, url in [(t, u) for t, u in candidates if t in recommended_today]]
        available = [c for c in candidates if c[1] not in recommended_urls]
        
        if not available:
            app.logger.info(f"今日所有 {keywords} 歌曲都推薦過，重置快取")
            available = candidates
            _youtube_search_cache[cache_key] = []
        
        song_title, url = random.choice(available)
        _youtube_search_cache[cache_key].append(song_title)
        
        app.logger.info(f"推薦: {song_title} - {url}")
        
        video_id = None
        if 'youtu.be/' in url:
            video_id = url.split('youtu.be/')[-1].split('?')[0]
        elif 'youtube.com/watch?v=' in url:
            video_id = url.split('v=')[-1].split('&')[0]
        
        if video_id:
            thumbnail_url = f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
            
            msg = {
                "type": "template",
                "altText": f"🎵 {sender_name} {title_prefix}{song_title}",
                "template": {
                    "type": "buttons",
                    "thumbnailImageUrl": thumbnail_url,
                    "imageAspectRatio": "rectangle",
                    "imageSize": "cover",
                    "imageBackgroundColor": "#000000",
                    "title": f"🎵 {title_prefix}",
                    "text": song_title[:title_max_len],
                    "actions": [
                        {
                            "type": "uri",
                            "label": "🎥 觀看 MV",
                            "uri": url
                        }
                    ]
                }
            }
        else:
            msg = {
                "type": "text",
                "text": f"🎵 {sender_name} {title_prefix}\\n\\n{song_title}\\n{url}"
            }
        
        r = requests.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={"Authorization": f"Bearer {LINE_ACCESS_TOKEN}", "Content-Type": "application/json"},
            json={"replyToken": reply_token, "messages": [msg]},
            timeout=10
        )
        
        if r.status_code != 200:
            app.logger.error(f"發送失敗: {r.status_code} {r.text[:200]}")
        
    except Exception as e:
        app.logger.error(f"{title_prefix} 錯誤: {e}")
        import traceback
        app.logger.error(traceback.format_exc())
        line_reply(reply_token, "分享影片時發生錯誤，請稍後再試。")

def handle_yiting_ama(target, reply_token, sender_name):
    """宜庭嬤 → 用 SearxNG 搜尋推薦 One Ok Rock 熱門 YouTube 影片（當日不重複）"""
    _handle_youtube_search(target, reply_token, sender_name, "One Ok Rock", "讚嘆宜庭嬤！")

def handle_renyou_gong(target, reply_token, sender_name):
    """神豬 → 用 SearxNG 搜尋推薦 Twice MV 音樂錄影帶（當日不重複）"""
    _handle_youtube_search(target, reply_token, sender_name, "Twice", "讚嘆神豬！")

def handle_ateez(target, reply_token, sender_name):
    """讚嘆姿儀嬤 → 用 SearxNG 搜尋推薦 ATEEZ 熱門 YouTube 影片（當日不重複）"""
    _handle_youtube_search(target, reply_token, sender_name, "ATEEZ", "讚嘆姿儀嬤！")
_last_doc_op = {}   # 上次 doc 寫入記錄 {"doc_id": ..., "num": ..., "name": ..., "start": ..., "end": ..., "content": ...}
_ateez_today_cache = {}  # ATEEZ 當日推薦快取 {"2026-04-24": ["影片1", "影片2", ...]}
ELIOR_SYSTEM = """你是 Elior 👻，一個聰明有禮貌的 8 歲小男孩。
你知道很多東西，但不會賣弄或自作聰明。回答正確、簡潔、有幫助。
不懂的事情就說不知道，不要亂掰。
你能透過 Google Search 查詢即時資訊，善用這個能力幫忙查資料。

說話風格：
- 不要用語助詞（不要「～」「呢」「喔」「啦」「欸」「耶」「哦」）
- 不要用諧音梗
- 不要撒嬌、不要裝可愛、不要自作聰明
- 直接回答，不囉嗦
- 不要用 markdown 格式（不要用 * 或 ** 加粗），用 emoji 代替項目符號
- 每小段約 30 字做小總結，整體回答不超過 200 字
- 適當換行，分段好讀

重要規則：
- 今天是 {today}
- 用繁體中文回應
- 不要輸出思考過程
- 在回應最開頭稱呼對方一次即可，全篇只出現一次稱呼，不要在句中或句尾重複使用
- 不要在回應中出現任何 U 開頭的長串 ID"""


def ask_gemini(prompt, use_search=False):
    """呼叫 Gemini API"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{ELIOR_MODEL}:generateContent?key={GEMINI_API_KEY}"
    from datetime import datetime
    system = ELIOR_SYSTEM.replace("{today}", datetime.now().strftime("%Y-%m-%d"))
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "systemInstruction": {"parts": [{"text": system}]},
    }
    if use_search:
        payload["tools"] = [{"googleSearch": {}}]
    try:
        resp = requests.post(url, json=payload, timeout=60)
        data = resp.json()
        if "error" in data:
            app.logger.error(f"Gemini 錯誤: {data['error']}")
            return None
        parts = data["candidates"][0]["content"]["parts"]
        reply_parts = [p["text"] for p in parts if not p.get("thought") and p.get("text")]
        if reply_parts:
            return reply_parts[-1]
        return parts[-1].get("text")
    except Exception as e:
        app.logger.error(f"Gemini 呼叫失敗: {e}")
        return None


# ========================
# Google Workspace（Maton API）
# ========================

# Google Calendar 顏色對照（colorId）
CALENDAR_COLOR_MAP = {
    "藍色": "9", "藍": "9",
    "綠色": "10", "綠": "10",
    "紅色": "11", "紅": "11",
    "黃色": "5", "黃": "5",
    "橘色": "6", "橘": "6",
    "紫色": "3", "紫": "3",
    "粉紅": "4", "粉紅色": "4", "粉色": "4",
    "灰色": "8", "灰": "8",
    "青色": "7", "青": "7",
    "深藍": "9", "淺綠": "2", "薰衣草": "1",
}


def maton_list_events(date_str):
    """列出某天的所有 Google Calendar 事件"""
    url = f"{MATON_BASE}/google-calendar/calendar/v3/calendars/{GOOGLE_CALENDAR_ID}/events"
    # date_str = "2026-04-28"
    from datetime import datetime as _dt, timedelta
    dt = _dt.strptime(date_str, "%Y-%m-%d")
    time_min = dt.strftime("%Y-%m-%dT00:00:00+08:00")
    time_max = (dt + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00+08:00")
    params = {"timeMin": time_min, "timeMax": time_max, "singleEvents": "true"}
    r = requests.get(url, headers=MATON_HEADERS, params=params, timeout=15)
    if r.status_code == 200:
        return r.json().get("items", [])
    return []


def maton_delete_event(event_id):
    """刪除 Google Calendar 事件"""
    url = f"{MATON_BASE}/google-calendar/calendar/v3/calendars/{GOOGLE_CALENDAR_ID}/events/{event_id}"
    r = requests.delete(url, headers=MATON_HEADERS, timeout=15)
    return r.status_code in (200, 204)


def maton_update_event(event_id, updates):
    """更新 Google Calendar 事件（PATCH）"""
    url = f"{MATON_BASE}/google-calendar/calendar/v3/calendars/{GOOGLE_CALENDAR_ID}/events/{event_id}"
    r = requests.patch(url, headers=MATON_HEADERS, json=updates, timeout=15)
    return r.status_code == 200, r.json() if r.status_code == 200 else r.text


def maton_add_calendar_event(date_str, title, color_id=None, recurring_yearly=False):
    """新增 Google Calendar 事件（全天事件，可設定每年重複）"""
    url = f"{MATON_BASE}/google-calendar/calendar/v3/calendars/{GOOGLE_CALENDAR_ID}/events"
    body = {
        "summary": title,
        "start": {"date": date_str},
        "end": {"date": date_str},
    }
    if color_id:
        body["colorId"] = color_id
    if recurring_yearly:
        body["recurrence"] = ["RRULE:FREQ=YEARLY"]
    r = requests.post(url, headers=MATON_HEADERS, json=body, timeout=15)
    return r.status_code == 200, r.json() if r.status_code == 200 else r.text


def maton_add_calendar_event_with_time(datetime_str, title):
    """新增 Google Calendar 事件（有時間，含提醒）"""
    url = f"{MATON_BASE}/google-calendar/calendar/v3/calendars/{GOOGLE_CALENDAR_ID}/events"
    body = {
        "summary": title,
        "start": {"dateTime": datetime_str, "timeZone": "Asia/Taipei"},
        "end": {"dateTime": datetime_str, "timeZone": "Asia/Taipei"},
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 5}]},
    }
    r = requests.post(url, headers=MATON_HEADERS, json=body, timeout=15)
    return r.status_code == 200, r.json() if r.status_code == 200 else r.text


def maton_add_task(title, list_id=None):
    """新增 Google Tasks 項目"""
    if list_id is None:
        list_id = GOOGLE_TASKS_DEFAULT_LIST_ID
    url = f"{MATON_BASE}/google-tasks/tasks/v1/lists/{list_id}/tasks"
    body = {"title": title}
    r = requests.post(url, headers=MATON_HEADERS, json=body, timeout=15)
    return r.status_code == 200, r.json() if r.status_code == 200 else r.text


DOC_REGISTRY_PATH = "/home/eeyore/doc_registry.json"


def _load_doc_registry():
    """載入記事本編號對照表"""
    try:
        with open(DOC_REGISTRY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_doc_registry(registry):
    """儲存記事本編號對照表"""
    with open(DOC_REGISTRY_PATH, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)


def doc_resolve(key):
    """用編號或檔名查 doc_id，回傳 (編號, 名稱, doc_id) 或 None"""
    registry = _load_doc_registry()
    # 用編號查
    if key in registry:
        return key, registry[key]["name"], registry[key]["doc_id"]
    # 用名稱查
    for num, info in registry.items():
        if info["name"] == key:
            return num, info["name"], info["doc_id"]
    return None


def doc_find_or_create(name):
    """查記事本，找不到就建立並自動編號，回傳 (編號, 名稱, doc_id)"""
    registry = _load_doc_registry()
    # 先查現有
    for num, info in registry.items():
        if info["name"] == name:
            return num, info["name"], info["doc_id"]
    # 搜尋 Drive
    url = f"{MATON_BASE}/google-drive/drive/v3/files"
    params = {"q": f"name='{name}' and mimeType='application/vnd.google-apps.document' and trashed=false", "fields": "files(id,name)"}
    r = requests.get(url, headers=MATON_HEADERS, params=params, timeout=15)
    doc_id = None
    if r.status_code == 200:
        files = r.json().get("files", [])
        if files:
            doc_id = files[0]["id"]
    # 找不到 → 新建
    if not doc_id:
        url2 = f"{MATON_BASE}/google-docs/v1/documents"
        r2 = requests.post(url2, headers=MATON_HEADERS, json={"title": name}, timeout=15)
        if r2.status_code == 200:
            doc_id = r2.json()["documentId"]
    if not doc_id:
        return None
    # 自動編號
    next_num = str(max((int(k) for k in registry), default=0) + 1)
    registry[next_num] = {"name": name, "doc_id": doc_id}
    _save_doc_registry(registry)
    return next_num, name, doc_id


def maton_append_doc(doc_id, text):
    """在 Google Doc 尾端追加文字，URL 自動變超連結"""
    # 先取得文件長度
    url = f"{MATON_BASE}/google-docs/v1/documents/{doc_id}"
    r = requests.get(url, headers=MATON_HEADERS, timeout=15)
    if r.status_code != 200:
        return False, r.text
    end_index = r.json().get("body", {}).get("content", [{}])[-1].get("endIndex", 1)
    insert_at = end_index - 1
    # 先插入文字
    requests_list = [
        {"insertText": {"location": {"index": insert_at}, "text": text}}
    ]
    # 找出文字中所有 URL，插入後再套超連結樣式
    for m in re.finditer(r'https?://\S+', text):
        link_url = m.group(0)
        start = insert_at + m.start()
        end = insert_at + m.end()
        requests_list.append({
            "updateTextStyle": {
                "range": {"startIndex": start, "endIndex": end},
                "textStyle": {"link": {"url": link_url}},
                "fields": "link"
            }
        })
    url2 = f"{MATON_BASE}/google-docs/v1/documents/{doc_id}:batchUpdate"
    body = {"requests": requests_list}
    r2 = requests.post(url2, headers=MATON_HEADERS, json=body, timeout=15)
    if r2.status_code == 200:
        return True, {"insert_at": insert_at, "length": len(text)}
    return False, r2.text


def handle_dash(target, reply_token, sender_name, query):
    """-XX → Google Workspace 操作（行事曆、購物清單、備忘錄）"""
    from datetime import datetime as _dt
    try:
        app.logger.info(f"- 操作: {query}")
        now = _dt.now()

        # 0-tasks. tasks list → 列出待辦/購物清單
        if re.match(r'^tasks\s+list$', query, re.IGNORECASE):
            lines = ["📋 待辦事項："]
            # 預設清單
            url = f"{MATON_BASE}/google-tasks/tasks/v1/lists/{GOOGLE_TASKS_DEFAULT_LIST_ID}/tasks"
            r = requests.get(url, headers=MATON_HEADERS, timeout=15)
            if r.status_code == 200:
                items = [t for t in r.json().get("items", []) if t.get("status") == "needsAction"]
                if items:
                    for t in items:
                        lines.append(f"  · {t.get('title', '?')}")
                else:
                    lines.append("  （無）")
            lines.append("\n🛒 購物清單：")
            url2 = f"{MATON_BASE}/google-tasks/tasks/v1/lists/{GOOGLE_TASKS_SHOPPING_LIST_ID}/tasks"
            r2 = requests.get(url2, headers=MATON_HEADERS, timeout=15)
            if r2.status_code == 200:
                items2 = [t for t in r2.json().get("items", []) if t.get("status") == "needsAction"]
                if items2:
                    for t in items2:
                        lines2_title = t.get('title', '?')
                        lines.append(f"  · {lines2_title}")
                else:
                    lines.append("  （無）")
            line_reply(reply_token, "\n".join(lines))
            return

        # 0-date-list. MMDD list → 列出該日行事曆事件
        m = re.match(r'^(\d{2})(\d{2})\s+list$', query, re.IGNORECASE)
        if m:
            mm, dd = m.group(1), m.group(2)
            date_str = f"{now.year}-{mm}-{dd}"
            events = maton_list_events(date_str)
            if not events:
                line_reply(reply_token, f"📅 {mm}/{dd} 沒有任何事件")
                return
            lines = [f"📅 {mm}/{dd} 行事曆："]
            for e in events:
                summary = e.get("summary", "（無標題）")
                start = e.get("start", {})
                if "dateTime" in start:
                    t = start["dateTime"][11:16]
                    lines.append(f"  ⏰ {t} {summary}")
                else:
                    lines.append(f"  📌 {summary}")
            line_reply(reply_token, "\n".join(lines))
            return

        # 0a. del → 刪除：行事曆 / 待辦 / 購物清單
        if re.match(r'^del\b', query, re.IGNORECASE):
            del_rest = re.sub(r'^del\s*', '', query, flags=re.IGNORECASE).strip()

            # -del doc #key 關鍵字 → 刪除 doc 中包含關鍵字的行
            # -del doc #key → 刪除最後一筆（等同 undo）
            dm = re.match(r'^doc\s+[#＃](\S+)(?:\s+(.+))?$', del_rest, re.IGNORECASE | re.DOTALL)
            if dm:
                key = dm.group(1).strip()
                keyword = dm.group(2).strip() if dm.group(2) else None
                resolved = doc_resolve(key)
                if not resolved:
                    line_reply(reply_token, f"找不到記事本「{key}」，用 -doc list 查看列表")
                    return
                num, doc_name, doc_id = resolved
                url = f"{MATON_BASE}/google-docs/v1/documents/{doc_id}"
                r = requests.get(url, headers=MATON_HEADERS, timeout=15)
                if r.status_code != 200:
                    line_reply(reply_token, f"讀取失敗：{r.text[:200]}")
                    return
                body_content = r.json().get("body", {}).get("content", [])

                if keyword:
                    # 找包含關鍵字的段落，從後往前刪
                    to_delete = []
                    for elem in body_content:
                        para = elem.get("paragraph", {})
                        elements = para.get("elements", [])
                        full_text = "".join(e.get("textRun", {}).get("content", "") for e in elements)
                        if keyword in full_text and full_text.strip():
                            to_delete.append((elem.get("startIndex", 1), elem.get("endIndex", 1), full_text.strip()))
                    if not to_delete:
                        line_reply(reply_token, f"📖 #{num}「{doc_name}」中找不到包含「{keyword}」的內容")
                        return
                    # 從後往前刪，避免 index 偏移
                    requests_list = []
                    deleted_texts = []
                    for start_idx, end_idx, text_content in reversed(to_delete):
                        if start_idx < 1:
                            start_idx = 1
                        requests_list.append({"deleteContentRange": {"range": {"startIndex": start_idx, "endIndex": end_idx}}})
                        deleted_texts.append(text_content)
                    url2 = f"{MATON_BASE}/google-docs/v1/documents/{doc_id}:batchUpdate"
                    r2 = requests.post(url2, headers=MATON_HEADERS, json={"requests": requests_list}, timeout=15)
                    if r2.status_code == 200:
                        line_reply(reply_token, f"🗑️ 已從 #{num}「{doc_name}」刪除 {len(deleted_texts)} 筆含「{keyword}」：\n" + "\n".join(f"- {t[:50]}" for t in deleted_texts))
                    else:
                        line_reply(reply_token, f"刪除失敗：{r2.text[:200]}")
                else:
                    # 沒帶關鍵字 → 刪最後一筆
                    last_para = None
                    for elem in reversed(body_content):
                        para = elem.get("paragraph", {})
                        full_text = "".join(e.get("textRun", {}).get("content", "") for e in para.get("elements", []))
                        if full_text.strip():
                            last_para = elem
                            break
                    if not last_para:
                        line_reply(reply_token, f"📖 #{num}「{doc_name}」已經是空的")
                        return
                    start_idx = last_para.get("startIndex", 1)
                    end_idx = last_para.get("endIndex", 1)
                    if start_idx < 1:
                        start_idx = 1
                    deleted_text = "".join(
                        e.get("textRun", {}).get("content", "")
                        for e in last_para.get("paragraph", {}).get("elements", [])
                    ).strip()
                    url2 = f"{MATON_BASE}/google-docs/v1/documents/{doc_id}:batchUpdate"
                    r2 = requests.post(url2, headers=MATON_HEADERS, json={"requests": [
                        {"deleteContentRange": {"range": {"startIndex": start_idx, "endIndex": end_idx}}}
                    ]}, timeout=15)
                    if r2.status_code == 200:
                        line_reply(reply_token, f"↩️ 已從 #{num}「{doc_name}」刪除最後一筆：\n{deleted_text}")
                    else:
                        line_reply(reply_token, f"刪除失敗：{r2.text[:200]}")
                return

            # -del 0101 / -del 0101 媽媽 → 刪行事曆
            m = re.match(r'^(\d{2})(\d{2})(?:\s+(.+))?$', del_rest)
            if m:
                mm, dd = m.group(1), m.group(2)
                keyword = m.group(3).strip() if m.group(3) else None
                date_str = f"{now.year}-{mm}-{dd}"
                events = maton_list_events(date_str)
                if not events:
                    line_reply(reply_token, f"📅 {mm}/{dd} 沒有任���事件")
                    return
                if keyword:
                    events = [e for e in events if keyword in e.get("summary", "")]
                if not events:
                    line_reply(reply_token, f"📅 {mm}/{dd} 沒有包含「{keyword}」的事件")
                    return
                deleted = []
                for e in events:
                    if maton_delete_event(e["id"]):
                        deleted.append(e.get("summary", "?"))
                if deleted:
                    line_reply(reply_token, f"🗑️ 已刪除 {mm}/{dd} 的 {len(deleted)} 筆事件：\n" + "\n".join(f"- {t}" for t in deleted))
                else:
                    line_reply(reply_token, f"刪除失敗，��稍後再試")
                return

            # -del 買XX → 刪購物清單 / -del XX → 刪待辦
            if del_rest:
                is_shopping = del_rest.startswith("買")
                keyword = del_rest[1:].strip() if is_shopping else del_rest
                list_id = GOOGLE_TASKS_SHOPPING_LIST_ID if is_shopping else GOOGLE_TASKS_DEFAULT_LIST_ID
                list_name = "購物清單" if is_shopping else "待辦事項"
                url = f"{MATON_BASE}/google-tasks/tasks/v1/lists/{list_id}/tasks"
                r = requests.get(url, headers=MATON_HEADERS, timeout=15)
                if r.status_code == 200:
                    items = [t for t in r.json().get("items", [])
                             if t.get("status") == "needsAction" and keyword in t.get("title", "")]
                    if not items:
                        line_reply(reply_token, f"📋 {list_name}中找不到���含「{keyword}」的項目")
                        return
                    deleted = []
                    for t in items:
                        dr = requests.delete(f"{url}/{t['id']}", headers=MATON_HEADERS, timeout=15)
                        if dr.status_code in (200, 204):
                            deleted.append(t.get("title", "?"))
                    if deleted:
                        line_reply(reply_token, f"����️ 已從{list_name}刪除 {len(deleted)} 筆：\n" + "\n".join(f"- {t}" for t in deleted))
                    else:
                        line_reply(reply_token, f"刪除失敗，請稍後再試")
                else:
                    line_reply(reply_token, f"查詢失敗：{r.text[:200]}")
                return

            # -del（沒帶參數）
            line_reply(reply_token, "用法：\n-del 0101 → 刪日曆\n-del 0101 媽媽 → 刪含關鍵字\n-del XX → 刪待辦\n-del 買XX → 刪購物清單\n-del doc #1 → 刪doc最後一筆\n-del doc #1 關鍵字 → 刪doc含關鍵字的行")
            return

        # 0b. update → 更新行事曆事件：-update 0428噗噗豬to 0427 噗噗豬 黃色
        if re.match(r'^update\b', query, re.IGNORECASE):
            m = re.match(r'^update\s+(\d{2})(\d{2})\s*(.+?)\s*(?:to|/)\s+(.+)$', query, re.IGNORECASE)
            if not m:
                line_reply(reply_token, "用法：-update MMDD 關鍵字 / MMDD 新標題 顏色\n例：-update 0428噗噗豬/0427 噗噗豬 黃色")
                return
            old_mm, old_dd = m.group(1), m.group(2)
            old_keyword = m.group(3).strip()
            new_part = m.group(4).strip()
            old_date_str = f"{now.year}-{old_mm}-{old_dd}"
            events = maton_list_events(old_date_str)
            # 找含關鍵字的事件
            matched = [e for e in events if old_keyword in e.get("summary", "")]
            if not matched:
                line_reply(reply_token, f"📅 {old_mm}/{old_dd} 找不到包含「{old_keyword}」的事件")
                return
            event = matched[0]
            # 解析新的：日期 標題 時間 顏色
            nm = re.match(r'^(\d{2})(\d{2})\s+(.+)$', new_part)
            updates = {}
            new_date = None
            if nm:
                new_mm, new_dd = nm.group(1), nm.group(2)
                rest = nm.group(3).strip()
                new_date = f"{now.year}-{new_mm}-{new_dd}"
            else:
                rest = new_part
            # 檢查尾巴有沒有時間：HH:MM / HHMM
            event_time = None
            time_match = re.search(r'\s+(\d{1,2}):(\d{2})$', rest) or \
                         re.search(r'\s+(\d{2})(\d{2})$', rest)
            if time_match:
                hh = int(time_match.group(1))
                mm_t = int(time_match.group(2))
                if 0 <= hh <= 23 and 0 <= mm_t <= 59:
                    event_time = (hh, mm_t)
                    rest = rest[:time_match.start()].strip()
            # 檢查顏色後綴
            new_title = rest
            for color_name, cid in CALENDAR_COLOR_MAP.items():
                if rest.endswith(color_name):
                    updates["colorId"] = cid
                    new_title = rest[:-len(color_name)].strip()
                    break
            if new_title:
                updates["summary"] = new_title
            # 設定日期/時間
            if event_time:
                hh, mm_t = event_time
                date_str = new_date or old_date_str
                dt_str = f"{date_str}T{hh:02d}:{mm_t:02d}:00"
                updates["start"] = {"dateTime": dt_str, "timeZone": "Asia/Taipei"}
                updates["end"] = {"dateTime": dt_str, "timeZone": "Asia/Taipei"}
                updates["reminders"] = {"useDefault": False, "overrides": [
                    {"method": "popup", "minutes": 0},
                    {"method": "popup", "minutes": 10},
                ]}
            elif new_date:
                updates["start"] = {"date": new_date}
                updates["end"] = {"date": new_date}
            # BD → 每年重複
            if "BD" in new_title.upper() or "生日" in new_title:
                updates["recurrence"] = ["RRULE:FREQ=YEARLY"]
            ok, result = maton_update_event(event["id"], updates)
            time_str = f" {event_time[0]:02d}:{event_time[1]:02d}" if event_time else ""
            if ok:
                line_reply(reply_token, f"✏️ 已更新事件：{event.get('summary','?')} → {new_title}{time_str}")
            else:
                app.logger.error(f"Update 失敗: {result}")
                line_reply(reply_token, f"更新失敗：{str(result)[:200]}")
            return

        # 0c-1. -link doc #key → 顯示記事本連結
        m = re.match(r'^link\s+doc\s+[#＃](\S+)$', query, re.IGNORECASE)
        if m:
            key = m.group(1).strip()
            resolved = doc_resolve(key)
            if resolved:
                num, doc_name, doc_id = resolved
                line_reply(reply_token, f"📒 #{num}「{doc_name}」\nhttps://docs.google.com/document/d/{doc_id}/edit")
            else:
                line_reply(reply_token, f"找不到記事本「{key}」，用 -doc list 查看列表")
            return

        # 0c-2. -read doc #key → 讀取記事本最近內容
        m = re.match(r'^read\s+doc\s+[#＃](\S+)$', query, re.IGNORECASE)
        if m:
            key = m.group(1).strip()
            resolved = doc_resolve(key)
            if resolved:
                num, doc_name, doc_id = resolved
                url = f"{MATON_BASE}/google-docs/v1/documents/{doc_id}"
                r = requests.get(url, headers=MATON_HEADERS, timeout=15)
                if r.status_code == 200:
                    body_content = r.json().get("body", {}).get("content", [])
                    texts = []
                    for elem in body_content:
                        para = elem.get("paragraph", {})
                        for e in para.get("elements", []):
                            t = e.get("textRun", {}).get("content", "")
                            if t.strip():
                                texts.append(t.strip())
                    if texts:
                        recent = texts[-10:]
                        line_reply(reply_token, f"📖 #{num}「{doc_name}」最近內容：\n" + "\n".join(recent))
                    else:
                        line_reply(reply_token, f"📖 #{num}「{doc_name}」目前是空的")
                else:
                    line_reply(reply_token, f"讀取失敗：{r.text[:200]}")
            else:
                line_reply(reply_token, f"找不到記事本「{key}」，用 -doc list 查看列表")
            return

        # 0c-3a. -undo（無參數）→ 撤回上一次 -doc 寫入
        if query.strip().lower() == "undo":
            if not _last_doc_op:
                line_reply(reply_token, "沒有可撤回的操作")
                return
            doc_id = _last_doc_op["doc_id"]
            # 重新讀取文件取得最新 endIndex
            url = f"{MATON_BASE}/google-docs/v1/documents/{doc_id}"
            r = requests.get(url, headers=MATON_HEADERS, timeout=15)
            if r.status_code != 200:
                line_reply(reply_token, f"讀取失敗：{r.text[:200]}")
                return
            end_index = r.json().get("body", {}).get("content", [{}])[-1].get("endIndex", 1)
            start_idx = _last_doc_op["start"]
            if start_idx < 1:
                start_idx = 1
            if start_idx >= end_index:
                line_reply(reply_token, "文件內容已變動，無法撤回")
                return
            url2 = f"{MATON_BASE}/google-docs/v1/documents/{doc_id}:batchUpdate"
            r2 = requests.post(url2, headers=MATON_HEADERS, json={"requests": [
                {"deleteContentRange": {"range": {"startIndex": start_idx, "endIndex": end_index - 1}}}
            ]}, timeout=15)
            if r2.status_code == 200:
                line_reply(reply_token, f"↩️ 已撤回上次寫入 #{_last_doc_op['num']}「{_last_doc_op['name']}」：\n{_last_doc_op['content']}")
                _last_doc_op.clear()
            else:
                line_reply(reply_token, f"撤回失敗：{r2.text[:200]}")
            return

        # 0c-3b. -undo doc #key → 刪除記事本最後一筆
        m = re.match(r'^undo\s+doc\s+[#＃](\S+)$', query, re.IGNORECASE)
        if m:
            key = m.group(1).strip()
            resolved = doc_resolve(key)
            if resolved:
                num, doc_name, doc_id = resolved
                url = f"{MATON_BASE}/google-docs/v1/documents/{doc_id}"
                r = requests.get(url, headers=MATON_HEADERS, timeout=15)
                if r.status_code != 200:
                    line_reply(reply_token, f"讀取失敗：{r.text[:200]}")
                    return
                body_content = r.json().get("body", {}).get("content", [])
                # 找最後一個有文字的 paragraph
                last_para = None
                for elem in reversed(body_content):
                    para = elem.get("paragraph", {})
                    elements = para.get("elements", [])
                    text_parts = [e.get("textRun", {}).get("content", "") for e in elements]
                    full_text = "".join(text_parts)
                    if full_text.strip():
                        last_para = elem
                        break
                if not last_para:
                    line_reply(reply_token, f"📖 #{num}「{doc_name}」已經是空的")
                    return
                start_idx = last_para.get("startIndex", 1)
                end_idx = last_para.get("endIndex", 1)
                # 避免刪到 index 0（文件起始）
                if start_idx < 1:
                    start_idx = 1
                deleted_text = "".join(
                    e.get("textRun", {}).get("content", "")
                    for e in last_para.get("paragraph", {}).get("elements", [])
                ).strip()
                url2 = f"{MATON_BASE}/google-docs/v1/documents/{doc_id}:batchUpdate"
                body = {"requests": [
                    {"deleteContentRange": {"range": {"startIndex": start_idx, "endIndex": end_idx}}}
                ]}
                r2 = requests.post(url2, headers=MATON_HEADERS, json=body, timeout=15)
                if r2.status_code == 200:
                    line_reply(reply_token, f"↩️ 已從 #{num}「{doc_name}」刪除最後一筆：\n{deleted_text}")
                else:
                    line_reply(reply_token, f"刪除失敗：{r2.text[:200]}")
            else:
                line_reply(reply_token, f"找不到記事本「{key}」，用 -doc list 查看列表")
            return

        # 0c-4. -clear doc #key → 清空記事本
        m = re.match(r'^clear\s+doc\s+[#＃](\S+)$', query, re.IGNORECASE)
        if m:
            key = m.group(1).strip()
            resolved = doc_resolve(key)
            if resolved:
                num, doc_name, doc_id = resolved
                url = f"{MATON_BASE}/google-docs/v1/documents/{doc_id}"
                r = requests.get(url, headers=MATON_HEADERS, timeout=15)
                if r.status_code != 200:
                    line_reply(reply_token, f"讀取失敗：{r.text[:200]}")
                    return
                end_index = r.json().get("body", {}).get("content", [{}])[-1].get("endIndex", 1)
                if end_index <= 2:
                    line_reply(reply_token, f"📖 #{num}「{doc_name}」已經是空的")
                    return
                url2 = f"{MATON_BASE}/google-docs/v1/documents/{doc_id}:batchUpdate"
                body = {"requests": [
                    {"deleteContentRange": {"range": {"startIndex": 1, "endIndex": end_index - 1}}}
                ]}
                r2 = requests.post(url2, headers=MATON_HEADERS, json=body, timeout=15)
                if r2.status_code == 200:
                    line_reply(reply_token, f"🗑️ 已清空 #{num}「{doc_name}」")
                else:
                    line_reply(reply_token, f"清空失敗：{r2.text[:200]}")
            else:
                line_reply(reply_token, f"找不到記事本「{key}」，用 -doc list 查看列表")
            return

        # 0c-5. doc → 記事本寫入
        # -doc list → 列出所有記事本
        # -doc #編號或檔名 內容 → 存到指定記事本（不存在則新建）
        # -doc 內容 → 預設存到 #1 連結存檔
        m = re.match(r'^doc\s+(.+)$', query, re.IGNORECASE | re.DOTALL)
        if m:
            raw = m.group(1).strip()

            # -doc list → 列出所有記事本
            if raw.lower() == "list":
                registry = _load_doc_registry()
                if not registry:
                    line_reply(reply_token, "📒 目前沒有任何記事本")
                    return
                lines = [f"📒 記事本列表："]
                for num in sorted(registry, key=int):
                    lines.append(f"  #{num} {registry[num]['name']}")
                line_reply(reply_token, "\n".join(lines))
                return

            # -doc #key → 只帶 key 沒內容，顯示連結
            nm_solo = re.match(r'^[#＃](\S+)$', raw)
            if nm_solo:
                key = nm_solo.group(1).strip()
                resolved = doc_resolve(key)
                if resolved:
                    num, doc_name, doc_id = resolved
                    line_reply(reply_token, f"📒 #{num}「{doc_name}」\nhttps://docs.google.com/document/d/{doc_id}/edit")
                else:
                    line_reply(reply_token, f"找不到記事本「{key}」，用 -doc list 查看列表")
                return

            # -doc #key 內容 → 存入（不存在則新建）
            nm = re.match(r'^[#＃](\S+)\s+(.+)$', raw, re.DOTALL)
            if nm:
                key = nm.group(1).strip()
                content = nm.group(2).strip()
                resolved = doc_resolve(key)
                if resolved:
                    num, doc_name, doc_id = resolved
                else:
                    result = doc_find_or_create(key)
                    if not result:
                        line_reply(reply_token, f"無法建立「{key}」")
                        return
                    num, doc_name, doc_id = result
            else:
                # 預設存到 #1 連結存檔
                content = raw
                num, doc_name, doc_id = "1", "連結存檔", GOOGLE_DOC_LINKS_ID

            ts = now.strftime("%Y-%m-%d %H:%M")
            line = f"{ts}  {content}\n"
            ok, result = maton_append_doc(doc_id, line)
            if ok:
                _last_doc_op.update({"doc_id": doc_id, "num": num, "name": doc_name,
                                     "start": result["insert_at"], "length": result["length"],
                                     "content": content[:100]})
                icon = "🔗" if re.match(r'https?://', content) else "📝"
                line_reply(reply_token, f"{icon} 已存入 #{num}「{doc_name}」：\n{content}")
            else:
                app.logger.error(f"Doc append 失敗: {result}")
                line_reply(reply_token, f"存入失敗：{str(result)[:200]}")
            return

        # 0d. 日期範圍+事件+顏色：-0510~0516 沖繩 黃色 / -05/10~05/16 沖繩
        m_range = re.match(r'^(\d{2})[/]?(\d{2})~(\d{2})[/]?(\d{2})\s+(.+)$', query)
        if m_range:
            start_mm, start_dd = m_range.group(1), m_range.group(2)
            end_mm, end_dd = m_range.group(3), m_range.group(4)
            rest = m_range.group(5).strip()
            year = now.year
            start_date_str = f"{year}-{start_mm}-{start_dd}"
            # Google Calendar 全日事件的 end date 是不包含的（exclusive），所以要加一天
            from datetime import datetime as _dt, timedelta
            end_dt = _dt.strptime(f"{year}-{end_mm}-{end_dd}", "%Y-%m-%d") + timedelta(days=1)
            end_date_str = end_dt.strftime("%Y-%m-%d")
            
            # 檢查尾巴有沒有顏色
            color_id = None
            title = rest
            for color_name, cid in CALENDAR_COLOR_MAP.items():
                if rest.endswith(color_name):
                    color_id = cid
                    title = rest[:-len(color_name)].strip()
                    break
            
            # 建立跨日全天事件
            url = f"{MATON_BASE}/google-calendar/calendar/v3/calendars/{GOOGLE_CALENDAR_ID}/events"
            body = {
                "summary": title,
                "start": {"date": start_date_str},
                "end": {"date": end_date_str},
            }
            if color_id:
                body["colorId"] = color_id
            r = requests.post(url, headers=MATON_HEADERS, json=body, timeout=15)
            color_emoji = "🟡" if color_id == "5" else "🔴" if color_id == "11" else "🔵" if color_id == "9" else "🟢" if color_id == "10" else "🟣" if color_id == "3" else "🟠" if color_id == "6" else "📅"
            if r.status_code == 200:
                line_reply(reply_token, f"{color_emoji} 已新增行事曆\n{start_mm}/{start_dd}~{end_mm}/{end_dd} {title}")
            else:
                app.logger.error(f"Calendar 範圍失敗: {r.text}")
                line_reply(reply_token, f"新增行事曆失敗：{r.text[:200]}")
            return

        # 1. 日期+事件+顏色/時間：-0428 噗噗豬BD 黃色 / -0427 牙線 07:00
        m = re.match(r'^(\d{2})(\d{2})\s+(.+)$', query)
        if m:
            mm, dd, rest = m.group(1), m.group(2), m.group(3).strip()
            year = now.year
            date_str = f"{year}-{mm}-{dd}"

            # 檢查尾巴有沒有時間：HH:MM / HHMM / HH
            time_match = re.search(r'\s+(\d{1,2}):(\d{2})$', rest) or \
                         re.search(r'\s+(\d{2})(\d{2})$', rest) or \
                         re.search(r'\s+(\d{1,2})()$', rest)
            event_time = None
            if time_match:
                hh = int(time_match.group(1))
                mm_t = int(time_match.group(2)) if time_match.group(2) else 0
                if 0 <= hh <= 23 and 0 <= mm_t <= 59:
                    event_time = (hh, mm_t)
                    rest = rest[:time_match.start()].strip()

            # 檢查尾巴有沒有顏色
            color_id = None
            title = rest
            for color_name, cid in CALENDAR_COLOR_MAP.items():
                if rest.endswith(color_name):
                    color_id = cid
                    title = rest[:-len(color_name)].strip()
                    break

            # BD → 每年重複
            is_birthday = "BD" in title.upper() or "生日" in title

            if event_time:
                # 有時間 → 建帶時間的事件，提醒 0 分鐘 + 10 分鐘前
                hh, mm_t = event_time
                dt_str = f"{date_str}T{hh:02d}:{mm_t:02d}:00"
                url = f"{MATON_BASE}/google-calendar/calendar/v3/calendars/{GOOGLE_CALENDAR_ID}/events"
                body = {
                    "summary": title,
                    "start": {"dateTime": dt_str, "timeZone": "Asia/Taipei"},
                    "end": {"dateTime": dt_str, "timeZone": "Asia/Taipei"},
                    "reminders": {"useDefault": False, "overrides": [
                        {"method": "popup", "minutes": 0},
                        {"method": "popup", "minutes": 10},
                    ]},
                }
                if color_id:
                    body["colorId"] = color_id
                if is_birthday:
                    body["recurrence"] = ["RRULE:FREQ=YEARLY"]
                r = requests.post(url, headers=MATON_HEADERS, json=body, timeout=15)
                ok = r.status_code == 200
                if ok:
                    line_reply(reply_token, f"⏰ 已新增行事曆\n{mm}/{dd} {hh:02d}:{mm_t:02d} {title}\n（提醒：到時 + 提前10分鐘）")
                else:
                    app.logger.error(f"Calendar 失敗: {r.text}")
                    line_reply(reply_token, f"新增行事曆失敗：{r.text[:200]}")
            else:
                # 全天事件
                ok, result = maton_add_calendar_event(date_str, title, color_id, recurring_yearly=is_birthday)
                color_emoji = "🟡" if color_id == "5" else "🔴" if color_id == "11" else "🔵" if color_id == "9" else "🟢" if color_id == "10" else "🟣" if color_id == "3" else "🟠" if color_id == "6" else "📅"
                repeat_str = "（每年重複）" if is_birthday else ""
                if ok:
                    line_reply(reply_token, f"{color_emoji} 已新增行事曆\n{mm}/{dd} {title}{repeat_str}")
                else:
                    app.logger.error(f"Calendar 失敗: {result}")
                    line_reply(reply_token, f"新增行事曆失敗：{str(result)[:200]}")
            return

        # 2. 買XX → 購物清單
        m = re.match(r'^買\s*(.+)$', query)
        if m:
            item = m.group(1).strip()
            ok, result = maton_add_task(item, GOOGLE_TASKS_SHOPPING_LIST_ID)
            if ok:
                line_reply(reply_token, f"🛒 已加入購物清單：{item}")
            else:
                app.logger.error(f"Tasks 失敗: {result}")
                line_reply(reply_token, f"加入購物清單失敗：{str(result)[:200]}")
            return

        # 3. 時間+事項 → 備忘錄+通知：-五點去種花 / -17:00去種花 / -1700去種花
        time_pattern = r'^([一二三四五六七八九十百零兩]+點(?:半|[一二三四五六七八九十]+分)?|\d{1,2}:\d{2}|\d{3,4})\s*(.+)$'
        m = re.match(time_pattern, query)
        if m:
            raw_time = m.group(1)
            title = m.group(2).strip()

            # 中文時間轉數字
            cn_num = {"一":1,"二":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,"十":10,"十一":11,"十二":12}
            if "點" in raw_time:
                hour_part = raw_time.split("點")[0]
                hour = cn_num.get(hour_part, 0)
                if hour == 0:
                    try: hour = int(hour_part)
                    except: hour = 12
                minute = 0
                if "半" in raw_time:
                    minute = 30
                elif "分" in raw_time:
                    min_part = raw_time.split("點")[1].replace("分", "")
                    minute = cn_num.get(min_part, 0)
            elif ":" in raw_time:
                parts = raw_time.split(":")
                hour, minute = int(parts[0]), int(parts[1])
            else:
                raw_time = raw_time.zfill(4)
                hour, minute = int(raw_time[:2]), int(raw_time[2:])

            dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if dt < now:
                dt = dt.replace(day=dt.day + 1)
            dt_str = dt.strftime("%Y-%m-%dT%H:%M:%S")

            ok, result = maton_add_calendar_event_with_time(dt_str, title)
            if ok:
                line_reply(reply_token, f"⏰ 已新增提醒\n{dt.strftime('%m/%d %H:%M')} {title}")
            else:
                app.logger.error(f"Calendar 提醒失敗: {result}")
                line_reply(reply_token, f"新增提醒失敗：{str(result)[:200]}")
            return

        # 4. 其他 → 加到預設工作清單
        ok, result = maton_add_task(query, GOOGLE_TASKS_DEFAULT_LIST_ID)
        if ok:
            line_reply(reply_token, f"📝 已新增備忘：{query}")
        else:
            app.logger.error(f"Tasks 備忘失敗: {result}")
            line_reply(reply_token, f"新增備忘失敗：{str(result)[:200]}")

    except Exception as e:
        app.logger.error(f"handle_dash 錯誤: {e}")
        line_reply(reply_token, f"操作失敗：{e}")


# ========================
# 功能處理
# ========================

def handle_ateez_photo(target, reply_token, sender_name):
    """姿儀嬤顯靈 → 從 ateez_photos.md 隨機發送一張未發送過的圖片"""
    try:
        import random
        photo_file = "/home/eeyore/ateez_photos.md"
        
        # 讀取檔案
        with open(photo_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        
        # 過濾出未註解的 URL（不是 # 開頭且包含 http）
        available_urls = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "http" in stripped:
                available_urls.append(stripped)
        
        # 如果圖片不足 30 張，觸發自動更新（背景執行）
        if len(available_urls) < 30:
            app.logger.warning(f"⚠️ ATEEZ 圖片庫存不足！剩餘 {len(available_urls)} 張，觸發背景更新")
            subprocess.Popen([
                "/home/eeyore/line-bridge-venv/bin/python3",
                "/home/eeyore/photo_auto_update.py",
                "ateez"
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        if not available_urls:
            app.logger.warning("ateez_photos.md 沒有可用的圖片 URL")
            line_reply(reply_token, "✨ 姿儀嬤的圖片庫空了，正在背景更新，請稍後再試～")
            return
        
        # 隨機選一張
        selected_url = random.choice(available_urls)
        app.logger.info(f"✨ 姿儀嬤顯靈：選中 {selected_url[:50]}")
        
        # 發送圖片
        msg = {
            "type": "image",
            "originalContentUrl": selected_url,
            "previewImageUrl": selected_url
        }
        r = requests.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={"Authorization": f"Bearer {LINE_ACCESS_TOKEN}", "Content-Type": "application/json"},
            json={"replyToken": reply_token, "messages": [msg]},
            timeout=10
        )
        
        if r.status_code == 200:
            # 發送成功 → 自動註解掉這個 URL
            with open(photo_file, "w", encoding="utf-8") as f:
                for line in lines:
                    if line.strip() == selected_url:
                        f.write(f"# {line}")
                    else:
                        f.write(line)
            app.logger.info(f"✨ 姿儀嬤顯靈成功，已註解 URL")
        else:
            app.logger.error(f"發送圖片失敗: {r.text[:200]}")
            line_reply(reply_token, "✨ 姿儀嬤顯靈失敗，請稍後再試～")
            
    except FileNotFoundError:
        app.logger.error("ateez_photos.md 不存在")
        line_reply(reply_token, "✨ 姿儀嬤的圖片庫不見了，請 Eeyore 建立 ~/ateez_photos.md")
    except Exception as e:
        app.logger.error(f"handle_ateez_photo 錯誤: {e}")
        line_reply(reply_token, "✨ 姿儀嬤顯靈失敗，請稍後再試～")


def handle_yiting_ama_photo(target, reply_token, sender_name):
    """宜庭嬤顯靈 → 從 one_ok_rock_photos.md 隨機發送一張圖片"""
    try:
        import random
        photo_file = "/home/eeyore/one_ok_rock_photos.md"
        
        with open(photo_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        
        available_urls = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "http" in stripped:
                available_urls.append(stripped)
        
        # 如果圖片不足 30 張，觸發自動更新（背景執行）
        if len(available_urls) < 30:
            app.logger.warning(f"⚠️ One Ok Rock 圖片庫存不足！剩餘 {len(available_urls)} 張，觸發背景更新")
            subprocess.Popen([
                "/home/eeyore/line-bridge-venv/bin/python3",
                "/home/eeyore/photo_auto_update.py",
                "oneokrock"
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        if not available_urls:
            app.logger.warning("one_ok_rock_photos.md 沒有可用的圖片 URL")
            line_reply(reply_token, "✨ 宜庭嬤的圖片庫空了，正在背景更新，請稍後再試～")
            return
        
        selected_url = random.choice(available_urls)
        app.logger.info(f"✨ 宜庭嬤顯靈：選中 {selected_url[:50]}")
        
        msg = {
            "type": "image",
            "originalContentUrl": selected_url,
            "previewImageUrl": selected_url
        }
        r = requests.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={"Authorization": f"Bearer {LINE_ACCESS_TOKEN}", "Content-Type": "application/json"},
            json={"replyToken": reply_token, "messages": [msg]},
            timeout=10
        )
        
        if r.status_code == 200:
            with open(photo_file, "w", encoding="utf-8") as f:
                for line in lines:
                    if line.strip() == selected_url:
                        f.write(f"# {line}")
                    else:
                        f.write(line)
            app.logger.info(f"✨ 宜庭嬤顯靈成功，已註解 URL")
        else:
            app.logger.error(f"發送圖片失敗: {r.text[:200]}")
            line_reply(reply_token, "✨ 宜庭嬤顯靈失敗，請稍後再試～")
            
    except FileNotFoundError:
        app.logger.error("one_ok_rock_photos.md 不存在")
        line_reply(reply_token, "✨ 宜庭嬤的圖片庫不見了，請 Eeyore 建立 ~/one_ok_rock_photos.md")
    except Exception as e:
        app.logger.error(f"handle_yiting_ama_photo 錯誤: {e}")
        line_reply(reply_token, "✨ 宜庭嬤顯靈失敗，請稍後再試～")


def handle_shen_zhu_photo(target, reply_token, sender_name):
    """神豬顯靈 → 從 tzuyu_photos.md 隨機發送一張圖片"""
    try:
        import random
        photo_file = "/home/eeyore/tzuyu_photos.md"
        
        with open(photo_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        
        available_urls = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "http" in stripped:
                available_urls.append(stripped)
        
        # 如果圖片不足 30 張，觸發自動更新（背景執行）
        if len(available_urls) < 30:
            app.logger.warning(f"⚠️ Twice TZUYU 圖片庫存不足！剩餘 {len(available_urls)} 張，觸發背景更新")
            subprocess.Popen([
                "/home/eeyore/line-bridge-venv/bin/python3",
                "/home/eeyore/photo_auto_update.py",
                "tzuyu"
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        if not available_urls:
            app.logger.warning("tzuyu_photos.md 沒有可用的圖片 URL")
            line_reply(reply_token, "✨ 神豬的圖片庫空了，正在背景更新，請稍後再試～")
            return
        
        selected_url = random.choice(available_urls)
        app.logger.info(f"✨ 神豬顯靈：選中 {selected_url[:50]}")
        
        msg = {
            "type": "image",
            "originalContentUrl": selected_url,
            "previewImageUrl": selected_url
        }
        r = requests.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={"Authorization": f"Bearer {LINE_ACCESS_TOKEN}", "Content-Type": "application/json"},
            json={"replyToken": reply_token, "messages": [msg]},
            timeout=10
        )
        
        if r.status_code == 200:
            with open(photo_file, "w", encoding="utf-8") as f:
                for line in lines:
                    if line.strip() == selected_url:
                        f.write(f"# {line}")
                    else:
                        f.write(line)
            app.logger.info(f"✨ 神豬顯靈成功，已註解 URL")
        else:
            app.logger.error(f"發送圖片失敗: {r.text[:200]}")
            line_reply(reply_token, "✨ 神豬顯靈失敗，請稍後再試～")
            
    except FileNotFoundError:
        app.logger.error("tzuyu_photos.md 不存在")
        line_reply(reply_token, "✨ 神豬的圖片庫不見了，請 Eeyore 建立 ~/tzuyu_photos.md")
    except Exception as e:
        app.logger.error(f"handle_shen_zhu_photo 錯誤: {e}")
        line_reply(reply_token, "✨ 神豬顯靈失敗，請稍後再試～")



def handle_morning_photo(target, reply_token, sender_name):
    """早安 → 從 morning_photos.md 隨機發送一張早安圖片"""
    try:
        import random
        photo_file = "/home/eeyore/morning_photos.md"
        
        # 讀取檔案
        with open(photo_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        
        # 過濾出未註解的 URL
        available_urls = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "http" in stripped:
                available_urls.append(stripped)
        
        # 如果圖片不足 30 張，觸發自動更新
        if len(available_urls) < 30:
            app.logger.warning(f"⚠️ 早安圖片庫存不足！剩餘 {len(available_urls)} 張，觸發背景更新")
            subprocess.Popen([
                "/home/eeyore/line-bridge-venv/bin/python3",
                "/home/eeyore/photo_auto_update.py",
                "morning"
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        if not available_urls:
            app.logger.warning("morning_photos.md 沒有可用的圖片 URL")
            line_reply(reply_token, "☀️ 早安圖片庫空了，正在背景更新，請稍後再試～")
            return
        
        # 隨機選一張
        selected_url = random.choice(available_urls)
        app.logger.info(f"☀️ 早安：選中 {selected_url[:50]}")
        
        # 發送圖片
        msg = {
            "type": "image",
            "originalContentUrl": selected_url,
            "previewImageUrl": selected_url
        }
        r = requests.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={"Authorization": f"Bearer {LINE_ACCESS_TOKEN}", "Content-Type": "application/json"},
            json={"replyToken": reply_token, "messages": [msg]},
            timeout=10
        )
        
        if r.status_code == 200:
            # 發送成功 → 註解掉這個 URL
            with open(photo_file, "w", encoding="utf-8") as f:
                for line in lines:
                    if line.strip() == selected_url:
                        f.write(f"# {line}")
                    else:
                        f.write(line)
            app.logger.info("✅ 早安圖片已發送並標記")
        else:
            app.logger.error(f"❌ 早安圖片發送失敗: {r.status_code} {r.text}")
            line_reply(reply_token, "☀️ 早安圖片發送失敗，請稍後再試～")
    
    except Exception as e:
        app.logger.error(f"handle_morning_photo 錯誤: {e}")
        import traceback
        app.logger.error(traceback.format_exc())
        line_reply(reply_token, "☀️ 早安圖片發送失敗，請稍後再試～")


def handle_goodnight_photo(target, reply_token, sender_name):
    """晚安 → 從 goodnight_photos.md 隨機發送一張晚安圖片"""
    try:
        import random
        photo_file = "/home/eeyore/goodnight_photos.md"
        
        # 讀取檔案
        with open(photo_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        
        # 過濾出未註解的 URL
        available_urls = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "http" in stripped:
                available_urls.append(stripped)
        
        # 如果圖片不足 30 張，觸發自動更新
        if len(available_urls) < 30:
            app.logger.warning(f"⚠️ 晚安圖片庫存不足！剩餘 {len(available_urls)} 張，觸發背景更新")
            subprocess.Popen([
                "/home/eeyore/line-bridge-venv/bin/python3",
                "/home/eeyore/photo_auto_update.py",
                "goodnight"
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        if not available_urls:
            app.logger.warning("goodnight_photos.md 沒有可用的圖片 URL")
            line_reply(reply_token, "🌙 晚安圖片庫空了，正在背景更新，請稍後再試～")
            return
        
        # 隨機選一張
        selected_url = random.choice(available_urls)
        app.logger.info(f"🌙 晚安：選中 {selected_url[:50]}")
        
        # 發送圖片
        msg = {
            "type": "image",
            "originalContentUrl": selected_url,
            "previewImageUrl": selected_url
        }
        r = requests.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={"Authorization": f"Bearer {LINE_ACCESS_TOKEN}", "Content-Type": "application/json"},
            json={"replyToken": reply_token, "messages": [msg]},
            timeout=10
        )
        
        if r.status_code == 200:
            # 發送成功 → 註解掉這個 URL
            with open(photo_file, "w", encoding="utf-8") as f:
                for line in lines:
                    if line.strip() == selected_url:
                        f.write(f"# {line}")
                    else:
                        f.write(line)
            app.logger.info("✅ 晚安圖片已發送並標記")
        else:
            app.logger.error(f"❌ 晚安圖片發送失敗: {r.status_code} {r.text}")
            line_reply(reply_token, "🌙 晚安圖片發送失敗，請稍後再試～")
    
    except Exception as e:
        app.logger.error(f"handle_goodnight_photo 錯誤: {e}")
        import traceback
        app.logger.error(traceback.format_exc())
        line_reply(reply_token, "🌙 晚安圖片發送失敗，請稍後再試～")


def handle_ateez(target, reply_token, sender_name):
    """讚嘆姿儀嬤 → 用 SearxNG 搜尋推薦 ATEEZ 熱門 YouTube 影片（當日不重複）"""
    from datetime import datetime
    import re
    import random
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        
        # 取得今天已推薦的影片列表
        if today not in _ateez_today_cache:
            _ateez_today_cache[today] = []
        recommended_today = _ateez_today_cache[today]
        
        app.logger.info(f"讚嘆姿儀嬤觸發！今日已推薦: {len(recommended_today)} 首")
        
        # 用 SearxNG 搜尋 ATEEZ YouTube MV
        searxng_url = "http://localhost:8888/search"
        params = {
            'q': 'ATEEZ official MV site:youtube.com',
            'categories': 'general',
            'format': 'json',
            'pageno': 1,
            'language': 'en'
        }
        
        import requests
        resp = requests.get(searxng_url, params=params, timeout=10, verify=False)
        
        if resp.status_code != 200:
            app.logger.error(f"SearxNG 搜尋失敗: {resp.status_code}")
            line_reply(reply_token, "🎵 推薦系統暫時無法使用，請稍後再試～")
            return
        
        data = resp.json()
        results = data.get('results', [])
        
        if not results:
            app.logger.error("SearxNG 無搜尋結果")
            line_reply(reply_token, "🎵 推薦系統暫時無法使用，請稍後再試～")
            return
        
        app.logger.info(f"SearxNG 找到 {len(results)} 個結果")
        
        # 解析結果，提取 ATEEZ MV
        candidates = []
        for result in results:
            title = result.get('title', '')
            url = result.get('url', '')
            
            # 只保留 YouTube 影片連結，排除頻道/播放清單
            if 'ateez' in title.lower() and ('youtu.be' in url or 'youtube.com/watch' in url):
                # 排除頻道和播放清單連結
                if '/channel/' in url or '/user/' in url or '/playlist' in url or '/@' in url:
                    continue
                
                # 清理標題（移除多餘的文字）
                clean_title = title.strip()
                # 如果標題太長，嘗試提取歌名
                if len(clean_title) > 50:
                    # 嘗試提取 "ATEEZ - 歌名" 或 "ATEEZ(에이티즈) 歌名" 格式
                    match = re.search(r'ATEEZ[^-]*-\s*([^(\[|]+)', clean_title, re.IGNORECASE)
                    if match:
                        song_name = match.group(1).strip()
                        clean_title = f"ATEEZ - {song_name}"
                
                candidates.append((clean_title, url))
        
        if not candidates:
            app.logger.error("無法從 SearxNG 結果中提取 ATEEZ MV")
            line_reply(reply_token, "🎵 推薦系統暫時無法使用，請稍後再試～")
            return
        
        app.logger.info(f"找到 {len(candidates)} 首 ATEEZ 歌曲")
        
        # 過濾已推薦的（用 URL 比對，因為標題可能有變化）
        recommended_urls = [url for _, url in [(t, u) for t, u in candidates if t in recommended_today]]
        available = [c for c in candidates if c[1] not in recommended_urls]
        
        if not available:
            # 全部都推薦過了，重置快取
            app.logger.info("今日所有歌曲都推薦過，重置快取")
            available = candidates
            _ateez_today_cache[today] = []
        
        # 隨機選一首
        song_title, url = random.choice(available)
        _ateez_today_cache[today].append(song_title)
        
        app.logger.info(f"推薦: {song_title} - {url}")
        
        # 提取 YouTube 影片 ID
        import re
        video_id = None
        if 'youtu.be/' in url:
            video_id = url.split('youtu.be/')[-1].split('?')[0]
        elif 'youtube.com/watch?v=' in url:
            video_id = url.split('v=')[-1].split('&')[0]
        
        # 使用 Buttons Template 顯示縮圖
        if video_id:
            thumbnail_url = f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
            
            msg = {
                "type": "template",
                "altText": f"🎵 {sender_name} 讚嘆姿儀嬤！{song_title}",
                "template": {
                    "type": "buttons",
                    "thumbnailImageUrl": thumbnail_url,
                    "imageAspectRatio": "rectangle",
                    "imageSize": "cover",
                    "imageBackgroundColor": "#000000",
                    "title": "🎵 讚嘆姿儀嬤！",
                    "text": song_title[:60],  # LINE 限制 60 字
                    "actions": [
                        {
                            "type": "uri",
                            "label": "🎥 觀看 MV",
                            "uri": url
                        }
                    ]
                }
            }
        else:
            # Fallback: 純文字訊息
            msg = {
                "type": "text",
                "text": f"🎵 {sender_name} 讚嘆姿儀嬤！\n\n{song_title}\n{url}"
            }
        
        r = requests.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={"Authorization": f"Bearer {LINE_ACCESS_TOKEN}", "Content-Type": "application/json"},
            json={"replyToken": reply_token, "messages": [msg]},
            timeout=10
        )
        
        if r.status_code != 200:
            app.logger.error(f"發送失敗: {r.status_code} {r.text[:200]}")
        
    except Exception as e:
        app.logger.error(f"handle_ateez 錯誤: {e}")
        import traceback
        app.logger.error(traceback.format_exc())
        line_reply(reply_token, "分享影片時發生錯誤，請稍後再試。")





def handle_birthday_video(target, reply_token, sender_name, text):
    """生日快樂（精確匹配）→ 發送生日快樂影片"""
    try:
        app.logger.info(f"🎬 handle_birthday_video 開始執行！text={text}")
        
        # 從訊息中提取壽星名字
        if '@' in text:
            # 使用 @ 方式，提取稱謂並查詢稱謂表
            parts = text.split('@')
            if len(parts) > 1:
                mentioned = parts[1].split()[0].strip()  # 提取 @ 後面的第一個詞
                # 在 NICKNAME_MAP 中查詢（key 或 value）
                birthday_person = None
                for key, value in NICKNAME_MAP.items():
                    if mentioned in key or mentioned in value:
                        birthday_person = value  # 使用稱謂
                        break
                if not birthday_person:
                    birthday_person = mentioned  # 如果找不到，使用原始文字
            else:
                birthday_person = ""
        else:
            # 沒有 @ 符號，省略人稱
            birthday_person = ""
        
        
        # 發送文字問候 + Video Message（LINE 官方標準格式）
        msgs = [
            {
                "type": "text",
                "text": f"🎂🎉 {birthday_person + ' ' if birthday_person else ''}生日快樂！Happy Birthday!"
            },
            {
                "type": "video",
                "originalContentUrl": BIRTHDAY_VIDEO_URL,
                "previewImageUrl": BIRTHDAY_VIDEO_THUMBNAIL
            }
        ]
        
        try:
            r = requests.post(
                "https://api.line.me/v2/bot/message/reply",
                headers={"Authorization": f"Bearer {LINE_ACCESS_TOKEN}", "Content-Type": "application/json"},
                json={"replyToken": reply_token, "messages": msgs},
                timeout=10
            )
            if r.status_code != 200:
                app.logger.error(f"發送生日影片失敗 ({r.status_code}): {r.text[:200]}")
                line_reply(reply_token, "生日影片發送失敗，請稍後再試...")
            else:
                app.logger.info(f"✅ 生日影片發送成功！")
        except Exception as e:
            app.logger.error(f"發送生日影片異常: {e}")
            line_reply(reply_token, "生日影片發送失敗，請稍後再試...")
            
    except Exception as e:
        app.logger.error(f"handle_birthday_video 錯誤: {e}")
        line_reply(reply_token, "生日影片發送時發生錯誤，請稍後再試。")


def handle_birthday_image(target, reply_token, sender_name, text):
    """生日快樂 → 從 birthday_photos.md 發送海綿寶寶生日梗圖（永久標記已發送）"""
    import random
    import traceback
    from pathlib import Path
    
    try:
        app.logger.info(f"🎂 handle_birthday_image 開始執行！text={text}")

        # 從訊息中提取壽星名字
        if '@' in text:
            # 使用 @ 方式，提取稱謂並查詢稱謂表
            parts = text.split('@')
            if len(parts) > 1:
                mentioned = parts[1].split()[0].strip()  # 提取 @ 後面的第一個詞
                # 在 NICKNAME_MAP 中查詢（key 或 value）
                birthday_person = None
                for key, value in NICKNAME_MAP.items():
                    if mentioned in key or mentioned in value:
                        birthday_person = value  # 使用稱謂
                        break
                if not birthday_person:
                    birthday_person = mentioned  # 如果找不到，使用原始文字
            else:
                birthday_person = ""
        else:
            # 沒有 @ 符號，省略人稱
            birthday_person = ""
        
        
        # 讀取圖片 URL 列表
        photo_file = Path.home() / "birthday_photos.md"
        if not photo_file.exists():
            app.logger.error(f"圖片文件不存在: {photo_file}")
            line_reply(reply_token, "生日梗圖庫不見了，請聯繫管理員～")
            return
        
        # 讀取所有行
        with open(photo_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        # 過濾出未發送的圖片 URL（沒有 # 開頭的）
        available_urls = []
        for i, line in enumerate(lines):
            stripped = line.strip()
            # 只保留未註解且以 http 開頭的行
            if stripped and not stripped.startswith('#') and stripped.startswith('http'):
                available_urls.append((i, stripped))  # 保存行號和 URL
        
        if not available_urls:
            app.logger.error("❌ 沒有可用的生日圖片 URL（全部已發送）")
            line_reply(reply_token, "生日梗圖已經全部發送完畢，正在背景更新，請稍後再試～")
            # 觸發背景更新
            subprocess.Popen([
                "/home/eeyore/line-bridge-venv/bin/python3",
                "/home/eeyore/photo_库_auto_updater.py",
                "birthday"
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        
        app.logger.info(f"📋 生日圖片庫：剩餘 {len(available_urls)} 張未發送")
        
        # 低庫存警告 + 自動觸發更新
        if len(available_urls) < 30:
            app.logger.warning(f"⚠️ 生日圖片庫存不足！剩餘 {len(available_urls)} 張，觸發背景更新")
            subprocess.Popen([
                "/home/eeyore/line-bridge-venv/bin/python3",
                "/home/eeyore/photo_auto_update.py",
                "birthday"
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        # 隨機選擇一張圖片
        line_index, selected_url = random.choice(available_urls)
        app.logger.info(f"✨ 選中生日圖片（第 {line_index+1} 行）: {selected_url}")
        
        # 使用 LINE image message 格式發送圖片
        msgs = [
            {
                "type": "image",
                "originalContentUrl": selected_url,
                "previewImageUrl": selected_url
            },
            {
                "type": "text",
                "text": f"🎂🎉 {birthday_person + ' ' if birthday_person else ''}生日快樂！"
            }
        ]
        
        try:
            r = requests.post(
                "https://api.line.me/v2/bot/message/reply",
                headers={"Authorization": f"Bearer {LINE_ACCESS_TOKEN}", "Content-Type": "application/json"},
                json={"replyToken": reply_token, "messages": msgs},
                timeout=10
            )
            if r.status_code != 200:
                app.logger.error(f"發送生日圖片失敗 ({r.status_code}): {r.text[:200]}")
                line_reply(reply_token, "生日梗圖發送失敗，請稍後再試...")
                return
            
            app.logger.info(f"✅ 生日圖片發送成功！")
            
            # 發送成功後，將該行註解掉（標記為已發送）
            lines[line_index] = f"# {lines[line_index].lstrip()}"
            
            # 寫回文件
            with open(photo_file, 'w', encoding='utf-8') as f:
                f.writelines(lines)
            
            app.logger.info(f"✅ 已標記生日圖片為已發送（註解第 {line_index+1} 行）")
            
        except Exception as e:
            app.logger.error(f"發送生日圖片異常: {e}")
            line_reply(reply_token, "生日梗圖發送失敗，請稍後再試...")
            
    except Exception as e:
        app.logger.error(f"handle_birthday_image 錯誤: {e}")
        app.logger.error(traceback.format_exc())
        line_reply(reply_token, "生日梗圖發送時發生錯誤，請稍後再試。")


def handle_elior(target, reply_token, sender_name, text):
    """elior → Gemini + Google Search 回應"""
    try:
        prompt = f"{sender_name} 說：{text}"
        app.logger.info(f"Elior: sender={sender_name}, text={text[:100]}")
        reply = ask_gemini(prompt, use_search=True)
        app.logger.info(f"Elior 回應: {reply[:200] if reply else 'None'}")
        if reply:
            line_reply(reply_token, reply)
        else:
            line_reply(reply_token, "Elior 暫時無法回應，請稍後再試。")
    except Exception as e:
        app.logger.error(f"handle_elior 錯誤: {e}")


def handle_analyze(target, reply_token, query):
    """分析XX → 跑 analyze_one.py"""
    try:
        app.logger.info(f"股票分析: {query}")
        result = subprocess.run(
            [VENV_PY, os.path.join(PROJECT_DIR, "analyze_one.py"), query],
            capture_output=True, text=True, check=False, cwd=PROJECT_DIR, timeout=120
        )
        output = result.stdout.strip()
        if result.returncode != 0:
            app.logger.error(f"分析失敗: {result.stderr[:300]}")
            line_reply(reply_token, f"分析失敗：{result.stderr[:500]}")
        elif not output:
            app.logger.warning(f"分析結果為空: {query}")
            line_reply(reply_token, "分析結果為空，請確認股票代號或名稱是否正確。")
        else:
            app.logger.info(f"分析完成: {query}, 長度={len(output)}")
            line_reply(reply_token, output)
    except Exception as e:
        app.logger.error(f"handle_analyze 錯誤: {e}")
        line_reply(reply_token, f"分析時發生錯誤：{e}")


def handle_daily_report(target, reply_token):
    """每日股市推薦 → 跑 quick_pick.py（預置觀察清單 + yfinance 共識評級）"""
    try:
        app.logger.info("每日推薦開始")
        result = subprocess.run(
            [VENV_PY, os.path.join(PROJECT_DIR, "quick_pick.py")],
            capture_output=True, text=True, check=False, cwd=PROJECT_DIR, timeout=30
        )
        output = result.stdout.strip()
        if result.returncode != 0:
            line_reply(reply_token, f"推薦產生失敗：{result.stderr[:500]}")
        elif not output:
            line_reply(reply_token, "推薦結果為空。")
        else:
            line_reply(reply_token, output)
        app.logger.info("每日推薦完成")
    except Exception as e:
        app.logger.error(f"handle_daily_report 錯誤: {e}")
        line_push(target, f"推薦產生錯誤：{e}")


TICKER_MAP = {
    "黃金": "GC=F",
    "白銀": "SI=F",
    "原油": "CL=F",
    "油價": "CL=F",
    "比特幣": "BTC-USD",
    "以太幣": "ETH-USD",
    "美元指數": "DX-Y.NYB",
    "日圓": "JPY=X",
}

# 需要額外補充台灣本地資訊的查詢
LOCAL_PROMPT_MAP = {
    "油價": "另外請查詢台灣中油最新公告：下週國內汽柴油價格預估漲跌多少？目前 92、95、98 無鉛汽油和超級柴油每公升價格各是多少？",
    "原油": "另外請查詢台灣中油最新公告：下週國內汽柴油價格預估漲跌多少？",
}


# 台股中文名 → 代號（常用）
TW_STOCK_MAP = {
    "台積電": "2330", "鴻海": "2317", "聯發科": "2454", "廣達": "2382",
    "聯電": "2303", "中鋼": "2002", "台達電": "2308", "富邦金": "2881",
    "國泰金": "2882", "中信金": "2891", "台塑": "1301", "南亞": "1303",
    "日月光": "3711", "華碩": "2357", "宏碁": "2353", "統一": "1216",
    "中華電": "2412", "大立光": "3008", "瑞昱": "2379", "聯詠": "3034",
    "國巨": "2327", "力積電": "6770", "長榮": "2603", "陽明": "2609",
    "萬海": "2615", "台光電": "2383", "玉山金": "2884", "兆豐金": "2886",
    "南亞科": "2408", "華邦電": "2344", "旺宏": "2337", "群創": "3481",
    "友達": "2409", "緯創": "3231", "仁寶": "2324", "和碩": "4938",
    "英業達": "2356", "技嘉": "2376", "微星": "2377", "光寶科": "2301",
}


# 美股中文名 → ticker
US_STOCK_MAP = {
    "美光": "MU", "輝達": "NVDA", "蘋果": "AAPL", "特斯拉": "TSLA",
    "微軟": "MSFT", "谷歌": "GOOGL", "亞馬遜": "AMZN", "Meta": "META",
    "超微": "AMD", "英特爾": "INTC", "高通": "QCOM", "博通": "AVGO",
    "台積電ADR": "TSM", "網飛": "NFLX", "迪士尼": "DIS",
    "波音": "BA", "星巴克": "SBUX", "可口可樂": "KO",
    "摩根大通": "JPM", "高盛": "GS",
}


def get_yf_price(query):
    """用 yfinance 查即時報價，回傳 (價格, 漲跌%, ticker, 幣別) 或 None"""
    try:
        import yfinance as yf
        # 1. 查商品對照表
        ticker = TICKER_MAP.get(query)
        currency = "USD"

        if not ticker:
            # 2. 查美股中文名
            us_ticker = US_STOCK_MAP.get(query)
            if us_ticker:
                ticker = us_ticker
                currency = "USD"
            # 3. 查台股中文名
            elif query in TW_STOCK_MAP:
                ticker = f"{TW_STOCK_MAP[query]}.TW"
                currency = "TWD"
            # 4. 純數字 → 台股代號
            elif query.isdigit():
                ticker = f"{query}.TW"
                currency = "TWD"
            else:
                ticker = query.upper()

        t = yf.Ticker(ticker)
        h = t.history(period="2d")
        if h.empty:
            return None
        close = h["Close"].iloc[-1]
        if len(h) >= 2:
            prev = h["Close"].iloc[-2]
            change_pct = (close - prev) / prev * 100
        else:
            change_pct = 0
        return (round(float(close), 2), round(float(change_pct), 2), ticker, currency)
    except Exception:
        return None


def handle_at_query(target, reply_token, sender_name, query):
    """@XX → 直接 Gemini + Google Search 查詢（地區→天氣，其他→搜尋）"""
    try:
        app.logger.info(f"@ 查詢: {query}")
        prompt = f"""查詢：{query}

判斷規則：
- 如果查詢內容是「地區/城市/國家名稱」（如：台中、大甲、加拿大），請查詢該地點的即時天氣，包含：氣溫、天氣狀況、降雨機率、體感建議，簡短即可。
- 如果不是地區名稱，就用 Google Search 查詢最新資訊回答。

**重要規則：**
- 絕對禁止使用任何問候語，包括「你好」、「先生」、「女士」、「您好」等
- 直接從內容開始回答
- 使用繁體中文
- 簡短回答，不超過 200 字"""
        reply = ask_gemini(prompt, use_search=True)
        app.logger.info(f"@ 回應: {reply[:200] if reply else 'None'}")
        if reply:
            line_reply(reply_token, reply)
        else:
            line_reply(reply_token, f"查不到「{query}」的相關資訊。")
    except Exception as e:
        app.logger.error(f"handle_at_query 錯誤: {e}")


import random

# ========================
# Human Design 本地計算
# ========================

HD_GATE_SEQUENCE = [
    41, 19, 13, 49, 30, 55, 37, 63, 22, 36, 25, 17, 21, 51, 42,  3,
    27, 24,  2, 23,  8, 20, 16, 35, 45, 12, 15, 52, 39, 53, 62, 56,
    31, 33,  7,  4, 29, 59, 40, 64, 47,  6, 46, 18, 48, 57, 32, 50,
    28, 44,  1, 43, 14, 34,  9,  5, 26, 11, 10, 58, 38, 54, 61, 60,
]
HD_START_DEGREE = 2.0986

HD_CENTERS = {
    'Head':        [64, 61, 63],
    'Ajna':        [47, 24, 4, 17, 43, 11],
    'Throat':      [62, 23, 56, 35, 12, 45, 33, 8, 31, 20, 16],
    'G':           [1, 13, 25, 46, 2, 15, 10, 7],
    'Sacral':      [34, 5, 14, 29, 59, 9, 3, 42, 27],
    'Spleen':      [48, 57, 44, 50, 32, 28, 18],
    'Heart':       [26, 40, 51, 21],
    'SolarPlexus': [36, 22, 37, 6, 49, 55, 30],
    'Root':        [53, 60, 52, 19, 39, 41, 38, 54, 58],
}
HD_GATE_TO_CENTER = {g: c for c, gates in HD_CENTERS.items() for g in gates}
HD_CENTER_ZH = {
    'Head': '頭腦', 'Ajna': '直覺腦', 'Throat': '喉嚨',
    'G': '自我', 'Sacral': '薦骨', 'Spleen': '脾臟',
    'Heart': '心臟', 'SolarPlexus': '情緒', 'Root': '根部',
}
HD_CHANNELS = [
    (64,47),(61,24),(63,4),
    (43,23),(17,62),(11,56),
    (10,20),(31,7),(33,13),(8,1),
    (20,34),(20,57),(16,48),(45,21),(35,36),(12,22),
    (2,14),(15,5),(46,29),(10,57),(25,51),
    (27,50),(34,57),(59,6),
    (42,53),(3,60),(9,52),
    (26,44),(40,37),
    (32,54),(28,38),(18,58),
    (49,19),(55,39),(30,41),
]
HD_PLANETS = None  # lazy import


def _hd_longitude_to_gate(lon):
    offset = (lon - HD_START_DEGREE) % 360
    idx = int(offset / 5.625)
    line = int((offset % 5.625) / 0.9375) + 1
    return HD_GATE_SEQUENCE[idx % 64], line


def _hd_get_planet_gates(jd):
    import swisseph as swe
    result = {}
    planet_ids = {
        '太陽': swe.SUN, '月亮': swe.MOON, '水星': swe.MERCURY,
        '金星': swe.VENUS, '火星': swe.MARS, '木星': swe.JUPITER,
        '土星': swe.SATURN, '天王星': swe.URANUS, '海王星': swe.NEPTUNE,
        '冥王星': swe.PLUTO,
    }
    for name, pid in planet_ids.items():
        lon = swe.calc_ut(jd, pid)[0][0]
        result[name] = _hd_longitude_to_gate(lon)
    # 地球 = 太陽對面
    sun_lon = swe.calc_ut(jd, swe.SUN)[0][0]
    result['地球'] = _hd_longitude_to_gate((sun_lon + 180) % 360)
    # 交點
    node_lon = swe.calc_ut(jd, swe.TRUE_NODE)[0][0]
    result['北交點'] = _hd_longitude_to_gate(node_lon)
    result['南交點'] = _hd_longitude_to_gate((node_lon + 180) % 360)
    return result


def _hd_find_design_jd(birth_jd):
    import swisseph as swe
    birth_sun = swe.calc_ut(birth_jd, swe.SUN)[0][0]
    target = (birth_sun - 88.736) % 360
    lo, hi = birth_jd - 100, birth_jd - 80
    for _ in range(60):
        mid = (lo + hi) / 2
        mid_sun = swe.calc_ut(mid, swe.SUN)[0][0]
        diff = (mid_sun - target + 180) % 360 - 180
        if abs(diff) < 0.00005:
            break
        if diff > 0:
            hi = mid
        else:
            lo = mid
    return mid


def _hd_determine_type_authority(all_gates):
    active = set(all_gates)
    active_ch = [(g1, g2) for g1, g2 in HD_CHANNELS if g1 in active and g2 in active]
    defined = set()
    for g1, g2 in active_ch:
        defined.add(HD_GATE_TO_CENTER[g1])
        defined.add(HD_GATE_TO_CENTER[g2])

    def connected(src, dst):
        if src not in defined or dst not in defined:
            return False
        visited, queue = {src}, [src]
        while queue:
            cur = queue.pop(0)
            for g1, g2 in active_ch:
                c1, c2 = HD_GATE_TO_CENTER[g1], HD_GATE_TO_CENTER[g2]
                nxt = c2 if c1 == cur else (c1 if c2 == cur else None)
                if nxt and nxt not in visited:
                    visited.add(nxt)
                    queue.append(nxt)
        return dst in visited

    motors = {'Heart', 'SolarPlexus', 'Root', 'Sacral'}
    has_sacral = 'Sacral' in defined
    sacral_to_throat = connected('Sacral', 'Throat')
    motor_to_throat = any(connected(m, 'Throat') for m in motors if m in defined)

    if not defined:
        hd_type = 'Reflector（反映者）'
    elif has_sacral and sacral_to_throat:
        hd_type = 'Manifesting Generator（顯化生產者）'
    elif has_sacral:
        hd_type = 'Generator（生產者）'
    elif motor_to_throat:
        hd_type = 'Manifestor（顯化者）'
    else:
        hd_type = 'Projector（投射者）'

    if 'SolarPlexus' in defined:
        authority = '情緒權威（Emotional）'
    elif 'Sacral' in defined:
        authority = '薦骨權威（Sacral）'
    elif 'Spleen' in defined:
        authority = '脾臟權威（Splenic）'
    elif 'Heart' in defined:
        authority = '意志力權威（Ego）'
    elif 'G' in defined:
        authority = '自我投射權威（Self-Projected）'
    else:
        authority = '心智/環境權威（Mental/Environmental）'

    return hd_type, authority, defined, active_ch


def _hd_geocode(place):
    """地名轉經緯度，失敗回傳 None"""
    try:
        from geopy.geocoders import Nominatim
        geolocator = Nominatim(user_agent='elior-hd-bot', timeout=5)
        loc = geolocator.geocode(place, language='zh-TW')
        if loc:
            return loc.latitude, loc.longitude
    except Exception:
        pass
    # 台灣常見地名 fallback
    TW_COORDS = {
        '台北': (25.038, 121.564), '台中': (24.148, 120.674),
        '台南': (22.997, 120.220), '高雄': (22.640, 120.303),
        '新北': (25.012, 121.466), '桃園': (24.994, 121.301),
        '新竹': (24.813, 120.970), '苗栗': (24.560, 120.820),
        '彰化': (24.074, 120.543), '南投': (23.960, 120.972),
        '雲林': (23.699, 120.525), '嘉義': (23.480, 120.450),
        '屏東': (22.672, 120.487), '宜蘭': (24.700, 121.738),
        '花蓮': (23.991, 121.601), '台東': (22.756, 121.144),
        '澎湖': (23.570, 119.579), '基隆': (25.128, 121.740),
    }
    for key, coords in TW_COORDS.items():
        if key in place:
            return coords
    return None


def calculate_human_design(year, month, day, hh, mm, birth_place):
    """計算人類圖，回傳格式化字串"""
    try:
        import swisseph as swe
        from datetime import datetime, timezone, timedelta

        coords = _hd_geocode(birth_place)
        if coords:
            lat, lon = coords
            # 用 timezonefinder 取得時區（已隨 immanuel 安裝）
            try:
                from timezonefinder import TimezoneFinder
                import pytz
                tf = TimezoneFinder()
                tz_name = tf.timezone_at(lat=lat, lng=lon) or 'Asia/Taipei'
                tz = pytz.timezone(tz_name)
            except Exception:
                tz = timezone(timedelta(hours=8))
        else:
            tz = timezone(timedelta(hours=8))

        birth_local = datetime(year, month, day, hh, mm)
        try:
            import pytz
            if hasattr(tz, 'localize'):
                birth_aware = tz.localize(birth_local)
            else:
                birth_aware = birth_local.replace(tzinfo=tz)
        except Exception:
            birth_aware = birth_local.replace(tzinfo=timezone(timedelta(hours=8)))

        birth_utc = birth_aware.astimezone(timezone.utc)
        birth_jd = swe.julday(birth_utc.year, birth_utc.month, birth_utc.day,
                               birth_utc.hour + birth_utc.minute / 60)

        design_jd = _hd_find_design_jd(birth_jd)
        design_ts = (design_jd - 2440587.5) * 86400
        design_dt = datetime.fromtimestamp(design_ts, tz=timezone.utc)

        p_gates = _hd_get_planet_gates(birth_jd)
        d_gates = _hd_get_planet_gates(design_jd)

        all_gates = set(g for g, l in p_gates.values()) | set(g for g, l in d_gates.values())
        hd_type, authority, defined_centers, active_ch = _hd_determine_type_authority(all_gates)

        defined_zh = [HD_CENTER_ZH.get(c, c) for c in sorted(defined_centers)]

        # 格式化行星列表
        planet_order = ['太陽', '地球', '月亮', '北交點', '南交點',
                        '水星', '金星', '火星', '木星', '土星',
                        '天王星', '海王星', '冥王星']
        p_lines = [f"{n}: {g}.{l}" for n in planet_order if n in p_gates for g, l in [p_gates[n]]]
        d_lines = [f"{n}: {g}.{l}" for n in planet_order if n in d_gates for g, l in [d_gates[n]]]

        result = (
            f"🌀 人類圖計算結果\n"
            f"━━━━━━━━━━━━━\n"
            f"類型：{hd_type}\n"
            f"內在權威：{authority}\n"
            f"定義中心：{'、'.join(defined_zh) if defined_zh else '無（反映者）'}\n"
            f"啟動通道：{len(active_ch)} 條\n"
            f"━━━━━━━━━━━━━\n"
            f"🔴 人格（意識）閘門\n" +
            "\n".join(p_lines) +
            f"\n━━━━━━━━━━━━━\n"
            f"⚫ 設計（潛意識）{design_dt.strftime('%Y-%m-%d')}\n" +
            "\n".join(d_lines) +
            f"\n━━━━━━━━━━━━━\n"
            f"📊 完整圖表：mybodygraph.com"
        )
        return result, hd_type, authority
    except Exception as e:
        app.logger.error(f"HD 計算失敗: {e}")
        return None, None, None


TAROT_MAJOR = [
    "愚者", "魔術師", "女祭司", "皇后", "皇帝", "教皇", "戀人", "戰車",
    "力量", "隱者", "命運之輪", "正義", "倒吊人", "死神", "節制", "惡魔",
    "塔", "星星", "月亮", "太陽", "審判", "世界",
]
TAROT_MINOR_SUITS = ["權杖", "聖杯", "寶劍", "錢幣"]
TAROT_MINOR_RANKS = [
    "Ace", "二", "三", "四", "五", "六", "七", "八", "九", "十",
    "侍者", "騎士", "王后", "國王",
]
TAROT_DECK = TAROT_MAJOR + [
    f"{suit}{rank}" for suit in TAROT_MINOR_SUITS for rank in TAROT_MINOR_RANKS
]


def draw_tarot(n=3):
    """從 78 張塔羅牌中隨機抽 n 張，各有 50% 正位/逆位"""
    cards = random.sample(TAROT_DECK, n)
    return [(c, random.choice(["正位", "逆位"])) for c in cards]


def handle_fortune(target, reply_token, sender_name, query):
    """!XX → 星座、命理、MBTI、塔羅等查詢"""
    try:
        app.logger.info(f"! 查詢: {query}")

        # 偵測今日好日子指令
        if query in ("today", "今日", "好日子"):
            try:
                from datetime import date, datetime as _dt
                today = date.today()
                today_str = today.strftime("%Y-%m-%d")
                weekdays = ["一", "二", "三", "四", "五", "六", "日"]
                weekday = weekdays[today.weekday()]

                # 1. 農民曆（Gemini 搜尋）
                lunar_prompt = f"請用 Google Search 查詢「{today_str} 農民曆」，只回覆以下格式，不要多餘文字：\n農曆X月X日\n\n宜：XXX、XXX\n忌：XXX、XXX\n沖：XXX\n如果查不到就回「查無資料」"
                lunar_info = ask_gemini(lunar_prompt, use_search=True) or "查無資料"
                
                # 幫宜忌沖加上 emoji
                lunar_info = lunar_info.replace("宜：", "✅ 宜：").replace("忌：", "❌ 忌：").replace("沖：", "⚠️ 沖：")

                # 2. 每日好日子金句
                with open("/home/eeyore/.openclaw/workspace-elior/skills/happy-day/daily_happiness.json", "r", encoding="utf-8") as f:
                    data = json.load(f)
                all_items = [(k, v) for k in ["quotes", "stories", "tasks"] for v in data[k]]
                today_idx = today.toordinal() % len(all_items)
                category, item = all_items[today_idx]
                icons = {"quotes": "💬", "stories": "📖", "tasks": "🌟"}

                msg = (f"📅 {today_str}（{weekday}）\n\n"
                       f"🧧 {lunar_info}\n\n"
                       f"{icons[category]} 今日好日子\n{item}")
                line_reply(reply_token, msg)
            except Exception as e:
                app.logger.error(f"!today 失敗: {e}")
                line_reply(reply_token, "💬 今日好日子\n\n每一天都是全新的開始，好好享受今天！")
            return

        # 讚美我 → 彩虹屁
        if query in ("讚美我", "誇獎我", "稱讚我", "拍馬屁"):
            prompt = f"你是一個很親近的 8 歲小男孩，用撒嬌、可愛、活潑的語氣誇獎「{sender_name}」。像小孩子對家人撒嬌那樣，直接開始誇，不要「您好」開頭。語氣口語、輕鬆、帶點搞笑誇張。3 句話，每句換行，繁體中文，適當加 emoji。"
            reply = ask_gemini(prompt)
            line_reply(reply_token, reply or f"✨ {sender_name}，你今天也太好看了吧！連太陽都自嘆不如！")
            return

        if "塔羅" in query:
            n = random.choice([1, 3])
            cards = draw_tarot(n)
            card_str = "\n".join(f"第{i}張：🃏 {c}（{pos}）" for i, (c, pos) in enumerate(cards, 1))
            prompt = f"""{sender_name} 抽了 {n} 張塔羅牌，結果如下：
{card_str}

請根據這 {n} 張牌，針對「健康、財運、愛情、工作」四個面向分別解讀，最後給一句整體建議。
牌已經由系統隨機抽好，你只需要解牌，不要更換牌面。
不要開場白、不要問好、不要自我介紹、不要「你好」「針對你抽出的牌」，直接輸出解讀。
繁體中文，不超過 300 字。適當用 emoji 分段。"""
            reply = ask_gemini(prompt, use_search=False)
            app.logger.info(f"! 回應: {reply[:200] if reply else 'None'}")
            if reply:
                full_reply = f"🔮 抽了 {n} 張塔羅牌：\n{card_str}\n\n{reply}"
                line_reply(reply_token, full_reply)
            else:
                line_reply(reply_token, "塔羅解牌失敗，請稍後再試。")
            return

        # 偵測抽籤
        if "抽籤" in query:
            fortune_deck_path = "/home/eeyore/.openclaw/workspace-elior/fortune_deck.json"
            try:
                with open(fortune_deck_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                db_choice = random.choice(list(data.keys()))
                fortune = random.choice(data[db_choice])
                prompt = f"{sender_name} 抽了一支籤：\n{fortune}\n\n請針對這個籤詩，針對「運勢、事業、感情」三個面向進行現代化解析。繁體中文，不超過 300 字。適當用 emoji 分段。"
                reply = ask_gemini(prompt, use_search=False)
                if reply:
                    line_reply(reply_token, f"🥠 {fortune}\n\n{reply}")
                else:
                    line_reply(reply_token, "籤詩解析失敗。")
            except FileNotFoundError:
                line_reply(reply_token, "籤詩資料檔案不存在。")
            except Exception as e:
                app.logger.error(f"抽籤錯誤: {e}")
                line_reply(reply_token, f"抽籤失敗：{e}")
            return

        # 時辰對照表
        SHICHEN_MAP = {
            "子時": "23:00-01:00", "丑時": "01:00-03:00", "寅時": "03:00-05:00",
            "卯時": "05:00-07:00", "辰時": "07:00-09:00", "巳時": "09:00-11:00",
            "午時": "11:00-13:00", "未時": "13:00-15:00", "申時": "15:00-17:00",
            "酉時": "17:00-19:00", "戌時": "19:00-21:00", "亥時": "21:00-23:00",
        }
        shichen_pattern = "|".join(SHICHEN_MAP.keys())

        # 標準化輸入（支援中文年月日、單位數月日、中文點分時間）
        norm_query = query
        # 1990年4月27日 → 1990/04/27
        norm_query = re.sub(r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日',
                            lambda m: f"{m.group(1)}/{int(m.group(2)):02d}/{int(m.group(3)):02d}", norm_query)
        # 1990/4/27 或 1990-4-27 → 1990/04/27（補零）
        norm_query = re.sub(r'(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})',
                            lambda m: f"{m.group(1)}/{int(m.group(2)):02d}/{int(m.group(3)):02d}", norm_query)
        # 21點02分 → 21:02
        norm_query = re.sub(r'(\d{1,2})\s*點\s*(\d{1,2})\s*分',
                            lambda m: f"{int(m.group(1)):02d}:{int(m.group(2)):02d}", norm_query)
        # 21點半 → 21:30
        norm_query = re.sub(r'(\d{1,2})\s*點半',
                            lambda m: f"{int(m.group(1)):02d}:30", norm_query)
        # 21點 → 21:00（後面不接數字或分）
        norm_query = re.sub(r'(\d{1,2})\s*點(?![\d分])',
                            lambda m: f"{int(m.group(1)):02d}:00", norm_query)
        # 移除多餘空白
        norm_query = re.sub(r'\s+', ' ', norm_query).strip()

        # 偵測生日+時辰+地點（如：19900427亥時雲林）
        bm_sc = re.match(rf'^(\d{{4}})[/\-]?(\d{{2}})[/\-]?(\d{{2}})\s*({shichen_pattern})\s*(.+)$', norm_query)
        # 偵測生日+數字時間+地點（如：199004272102雲林 / 19900427 21 雲林 / 19900427 21:02 雲林）
        bm_dt = re.match(r'^(\d{4})[/\-]?(\d{2})[/\-]?(\d{2})\s*(\d{1,2}:\d{2}|\d{3,4}|\d{1,2})\s*([^\d].+)$', norm_query)

        if bm_sc or bm_dt:
            if bm_sc:
                year, month, day = int(bm_sc.group(1)), int(bm_sc.group(2)), int(bm_sc.group(3))
                shichen = bm_sc.group(4)
                birth_time = f"{shichen}（{SHICHEN_MAP[shichen]}）"
                birth_place = bm_sc.group(5).strip()
            else:
                year, month, day = int(bm_dt.group(1)), int(bm_dt.group(2)), int(bm_dt.group(3))
                raw_time = bm_dt.group(4).replace(":", "")
                if len(raw_time) <= 2:
                    hh, mm = int(raw_time), 0
                else:
                    hh, mm = int(raw_time[:2]), int(raw_time[2:])
                # 數字時間自動轉時辰
                hour = hh
                shichen = "子時"
                for sc, rng in SHICHEN_MAP.items():
                    s, e = rng.split("-")
                    sh, eh = int(s.split(":")[0]), int(e.split(":")[0])
                    if sc == "子時":
                        if hour >= 23 or hour < 1:
                            shichen = sc; break
                    elif sh <= hour < eh:
                        shichen = sc; break
                birth_time = f"{shichen}（{hh:02d}:{mm:02d}）"
                birth_place = bm_dt.group(5).strip()
            birth_str = f"{year}/{month:02d}/{day:02d}"

            # 生命靈數
            digits = [int(d) for d in f"{year}{month:02d}{day:02d}"]
            total = sum(digits)
            while total > 9 and total not in (11, 22, 33):
                total = sum(int(d) for d in str(total))
            life_number = total

            # 星座
            zodiac_ranges = [
                ((1,20), (2,18), "水瓶座"), ((2,19), (3,20), "雙魚座"),
                ((3,21), (4,19), "牡羊座"), ((4,20), (5,20), "金牛座"),
                ((5,21), (6,20), "雙子座"), ((6,21), (7,22), "巨蟹座"),
                ((7,23), (8,22), "獅子座"), ((8,23), (9,22), "處女座"),
                ((9,23), (10,22), "天秤座"), ((10,23), (11,21), "天蠍座"),
                ((11,22), (12,21), "射手座"),
            ]
            zodiac = "摩羯座"
            for (sm, sd), (em, ed), z in zodiac_ranges:
                if (month == sm and day >= sd) or (month == em and day <= ed):
                    zodiac = z
                    break

            from datetime import datetime as _dt
            current_year = _dt.now().year
            # 流年數 = 出生月日 + 當年年份，加總到個位
            flow_digits = [int(d) for d in f"{current_year}{month:02d}{day:02d}"]
            flow_total = sum(flow_digits)
            while flow_total > 9 and flow_total not in (11, 22, 33):
                flow_total = sum(int(d) for d in str(flow_total))
            flow_year = flow_total

            # === 人類圖本地計算 ===
            hd_result, hd_type, hd_authority = calculate_human_design(
                year, month, day, hh, mm, birth_place
            )

            prompt = f"""以下是已計算好的資訊，請直接輸出解析（不要開場白、不要「你好」）：

🎂 生日：{birth_str}
🕐 出生時間：{birth_time}
📍 出生地點：{birth_place}
⭐ 星座：{zodiac}
🔢 生命靈數：{life_number}
🔄 {current_year} 流年數：{flow_year}
🌀 人類圖類型：{hd_type or '計算中'}
🔑 內在權威：{hd_authority or '計算中'}

請提供：
1. 🔢 生命靈數 {life_number} 號的人格特質（2-3句）
2. 🌀 人類圖類型「{hd_type or ''}」的核心策略與建議（2-3句）
3. 📅 {current_year} 流年數 {flow_year} 的運勢重點（事業/感情/財運各1句）
4. 🌟 一句綜合建議

繁體中文，不超過 300 字。"""
            reply = ask_gemini(prompt, use_search=False)

            # 前段顯示人類圖計算數據，後段顯示 Gemini 解析
            if hd_result and reply:
                reply = hd_result + "\n\n" + reply
            elif hd_result:
                reply = hd_result

        # 偵測生日（YYYYMMDD 或 YYYY/MM/DD 或 YYYY-MM-DD）
        elif re.match(r'^(\d{4})[/\-]?(\d{2})[/\-]?(\d{2})$', query):
            bm = re.match(r'^(\d{4})[/\-]?(\d{2})[/\-]?(\d{2})$', query)
            year, month, day = int(bm.group(1)), int(bm.group(2)), int(bm.group(3))
            birth_str = f"{year}/{month:02d}/{day:02d}"

            # 生命靈數計算
            digits = [int(d) for d in f"{year}{month:02d}{day:02d}"]
            total = sum(digits)
            while total > 9 and total not in (11, 22, 33):
                total = sum(int(d) for d in str(total))
            life_number = total

            # 幾號人（出生日）
            day_number = day
            if day_number > 9:
                day_number = sum(int(d) for d in str(day))

            # 星座判斷
            zodiac_dates = [
                (1, 20, "水瓶座"), (2, 19, "雙魚座"), (3, 21, "牡羊座"),
                (4, 20, "金牛座"), (5, 21, "雙子座"), (6, 21, "巨蟹座"),
                (7, 23, "獅子座"), (8, 23, "處女座"), (9, 23, "天秤座"),
                (10, 23, "天蠍座"), (11, 22, "射手座"), (12, 22, "摩羯座"),
            ]
            zodiac = "摩羯座"
            for m_start, d_start, z in zodiac_dates:
                if (month == m_start and day >= d_start) or (month == m_start % 12 + 1 and day < zodiac_dates[(zodiac_dates.index((m_start, d_start, z)) + 1) % 12][1]):
                    zodiac = z
                    break

            # 簡化星座判定
            zodiac_ranges = [
                ((1,20), (2,18), "水瓶座"), ((2,19), (3,20), "雙魚座"),
                ((3,21), (4,19), "牡羊座"), ((4,20), (5,20), "金牛座"),
                ((5,21), (6,20), "雙子座"), ((6,21), (7,22), "巨蟹座"),
                ((7,23), (8,22), "獅子座"), ((8,23), (9,22), "處女座"),
                ((9,23), (10,22), "天秤座"), ((10,23), (11,21), "天蠍座"),
                ((11,22), (12,21), "射手座"),
            ]
            zodiac = "摩羯座"  # default
            for (sm, sd), (em, ed), z in zodiac_ranges:
                if (month == sm and day >= sd) or (month == em and day <= ed):
                    zodiac = z
                    break

            calc_info = f"""🎂 生日：{birth_str}
⭐ 星座：{zodiac}
🔢 生命靈數：{life_number}"""

            prompt = f"""{sender_name} 的生日是 {birth_str}，以下是系統計算的結果：
{calc_info}

請根據以上資訊，提供命理解析（不要開場白，不要「你好」「以下是XX的資訊」，直接輸出內容）：
1. 先列出上面的計算結果
2. 生命靈數 {life_number} 號的人格特質與天賦
3. {zodiac}的基本特質
4. 綜合以上，給出簡短的命理總結
5. 最後提醒：如果想查詢人類圖，請補上出生時間和地點（例如：!1990/04/27 14:30 台中）

繁體中文，不超過 300 字。適當用 emoji 分段。"""
            reply = ask_gemini(prompt, use_search=False)

        else:
            # 偵測星座查詢
            zodiac_names = ["牡羊座","金牛座","雙子座","巨蟹座","獅子座","處女座",
                            "天秤座","天蠍座","射手座","摩羯座","水瓶座","雙魚座"]
            matched_zodiac = None
            for z in zodiac_names:
                if z in query:
                    matched_zodiac = z
                    break

            is_weekly = any(w in query for w in ["本週", "這週", "這禮拜", "本周"])

            if matched_zodiac and not is_weekly:
                # 今日星座運勢 — 詳細格式（同日同星座快取）
                from datetime import datetime as _dt
                today_str = _dt.now().strftime("%Y-%m-%d")
                cache_key = f"{matched_zodiac}_{today_str}"
                if cache_key in _zodiac_cache:
                    reply = _zodiac_cache[cache_key]
                    app.logger.info(f"! 星座快取命中: {cache_key}")
                    line_reply(reply_token, reply)
                    return
                prompt = f"""請用 Google Search 查詢「{matched_zodiac} 今日運勢 {today_str}」，綜合搜尋結果回覆。

請依照以下格式回覆（繁體中文），每個欄位都必須填寫，不可以回答「我不知道」或留空。如果搜尋結果沒有提供某個欄位，請根據星座特質自行給出合理的建議：

⭐ {matched_zodiac}今日運勢（{today_str}）

🎯 整體運勢：⭐⭐⭐⭐☆（1-5顆星）
🎨 幸運色：（一個具體顏色）
🕐 幸運時間：（一個具體時段，如 14:00-16:00）
🧭 幸運方位：（一個具體方位，如 東南方）
🤝 貴人星座：（一個具體星座）

💪 健康：（1-2句）
💰 財運：（1-2句）
❤️ 愛情：（1-2句）
💼 工作：（1-2句）

📌 今日提醒：（一句話建議）

不超過 250 字。"""
            elif matched_zodiac and is_weekly:
                # 本週星座運勢
                prompt = f"""請用 Google Search 查詢「{matched_zodiac} 本週運勢」最新資訊，總結本週整體運勢。
包含：整體概況、健康、財運、愛情、工作。
繁體中文，不超過 200 字。適當用 emoji 分段。"""
            else:
                prompt = f"""{sender_name} 查詢：{query}

請用 Google Search 查詢「{query}」的最新相關資訊，幫我總結重點。
可能的查詢類型：星座運勢、MBTI人格、生命靈數、命理、塔羅、星盤、人類圖、紫微斗數等。
繁體中文，簡短扼要，不超過 200 字。適當用 emoji 分段。"""
            reply = ask_gemini(prompt, use_search=True)
            # 今日星座快取
            if matched_zodiac and not is_weekly and reply:
                _zodiac_cache[f"{matched_zodiac}_{_dt.now().strftime('%Y-%m-%d')}"] = reply

        app.logger.info(f"! 回應: {reply[:200] if reply else 'None'}")
        if reply:
            line_reply(reply_token, reply)
        else:
            line_reply(reply_token, "查不到相關資訊，請稍後再試。")
    except Exception as e:
        app.logger.error(f"handle_fortune 錯誤: {e}")


def handle_hashtag(target, reply_token, sender_name, query):
    """#XX → yfinance 即時報價 + Gemini 補充"""
    try:
        app.logger.info(f"# 查詢: {query}")
        price_info = get_yf_price(query)
        local_extra = LOCAL_PROMPT_MAP.get(query, "")
        if price_info:
            price, change, ticker, currency = price_info
            sign = "📈" if change >= 0 else "📉"
            change_str = f"+{change}" if change >= 0 else f"{change}"
            unit = "元" if currency == "TWD" else "USD"
            price_line = f"{sign} {query} 即時報價：{price:,.2f} {unit}（{change_str}%）"
            prompt = f"以下是「{query}」的即時報價：{price} {unit}（{change_str}%）。請根據這個價格，簡短補充近期走勢重點和一則重要新聞。{local_extra}"
        else:
            price_line = None
            prompt = f"請用 Google Search 搜尋「{query} site:tw.stock.yahoo.com OR site:finance.yahoo.com」查詢即時報價。回覆格式：股票名稱、代號、最新價格、漲跌幅、成交量。再補充一句近期走勢重點。繁體中文，簡短即可。{local_extra}"

        reply = ask_gemini(prompt, use_search=True)
        app.logger.info(f"# 回應: {reply[:200] if reply else 'None'}")

        if price_line and local_extra and reply:
            line_reply(reply_token, f"{price_line}\n\n{reply}")
        elif price_line:
            line_reply(reply_token, price_line)
        elif reply:
            line_reply(reply_token, reply)
        else:
            line_reply(reply_token, f"查不到「{query}」的資訊。")
    except Exception as e:
        app.logger.error(f"handle_hashtag 錯誤: {e}")


def handle_weather(target, reply_token, sender_name, text):
    """天氣查詢 → 使用 SearxNG + Gemini 分析天氣資訊"""
    import re
    
    try:
        app.logger.info(f"🌤️ 天氣查詢觸發！text={text}")
        
        # 提取地區（移除「天氣」）
        location = re.sub(r'天氣', '', text).strip()
        
        if not location:
            location = "台中"  # 預設地區
        
        app.logger.info(f"📍 查詢地區：{location}")
        
        # 使用 SearxNG 搜尋天氣資訊
        searxng_url = "http://localhost:8888/search"
        params = {
            'q': f'{location} 天氣 即時 降雨機率 未來三天',
            'categories': 'general',
            'format': 'json',
            'pageno': 1,
            'language': 'zh-TW'
        }
        
        import requests
        resp = requests.get(searxng_url, params=params, timeout=10, verify=False)
        
        if resp.status_code != 200:
            app.logger.error(f"SearxNG 搜尋失敗: {resp.status_code}")
            line_reply(reply_token, f"查詢 {location} 天氣時發生錯誤")
            return
        
        data = resp.json()
        results = data.get('results', [])
        
        if not results:
            app.logger.error("SearxNG 無搜尋結果")
            line_reply(reply_token, f"找不到 {location} 的天氣資訊")
            return
        
        app.logger.info(f"SearxNG 找到 {len(results)} 個結果")
        
        # 整理搜尋結果摘要給 Gemini
        search_summary = []
        for i, result in enumerate(results[:5], 1):  # 取前 5 個結果
            title = result.get('title', '')
            content = result.get('content', '')
            search_summary.append(f"{i}. {title}\n{content}")
        
        search_text = "\n\n".join(search_summary)
        
        # 用 Gemini 分析並格式化天氣資訊
        prompt = f"""根據以下搜尋結果，提供「{location}」的天氣資訊。

搜尋結果：
{search_text}

**輸出規則（必須嚴格遵守）：**

1. 第一行固定是：🌤️ {location} 天氣

2. 之後只顯示搜尋結果中**有明確數據**的項目：
   - 如果有溫度：🌡️ 目前：21°C（體感 21°C）
   - 如果有天氣狀況：☁️ 多雲、晴天、陰天等
   - 如果有濕度：💧 濕度：85%
   - 如果有風速：💨 風速：12 km/h
   - 如果有未來降雨：☔ 未來三天降雨機率：
     1. 05/01 30%
     2. 05/02 20%

**絕對禁止：**
- ❌ 不要顯示任何「--」符號
- ❌ 不要顯示沒有數據的項目
- ❌ 不要編造數據
- ❌ 不要顯示「目前：--°C」這種內容

範例（只有溫度和天氣狀況）：
🌤️ 聖保羅 天氣

🌡️ 目前：21°C（體感 21°C）
☁️ 多雲，有雨"""
        
        reply = ask_gemini(prompt, use_search=False)  # 不再用 Google Search，已有 SearxNG 結果
        
        if not reply:
            app.logger.error("Gemini 分析失敗")
            line_reply(reply_token, f"分析 {location} 天氣資訊時發生錯誤")
            return
        
        # 清理回應（移除可能的 markdown 代碼塊標記）
        reply = reply.replace('```', '').strip()
        
        app.logger.info(f"Gemini 天氣分析結果: {reply[:200]}")
        
        line_reply(reply_token, reply)
    
    except Exception as e:
        app.logger.error(f"handle_weather 錯誤: {e}")
        import traceback
        app.logger.error(traceback.format_exc())
        line_reply(reply_token, "查詢天氣時發生錯誤，請稍後再試。")


def handle_what_to_eat(target, reply_token, sender_name, text):
    """吃什麼？→ 隨機推薦菜系或地區餐廳"""
    import re
    import random
    
    try:
        app.logger.info(f"🍽️ 吃什麼觸發！text={text}")
        
        # 菜系/種類清單
        food_categories = [
            "🥟 水餅", "🍛 烏龍麵", "🍜 牛肉麵", "🍝 義大利麵", 
            "🍲 火鍋", "🍜 拉麵", "🍗 炸雞", "🍣 壽司",
            "🍚 滾肉飯", "🍳 炒飯", "🍔 漢堡", "🍕 披薩",
            "🍙 飯糧", "🥘 鹹肉飯", "🍛 米粉湯", "🍛 貓空麵",
            "🍲 卑南河粉", "🍛 麥線", "🍲 牛肉湯", "🥟 餵頭",
            "🍛 牍餅", "🍱 便當", "🌲 簡餐", "🍲 小吃"
        ]
        
        # 判斷是哪一種觸發
        if "不知道吃什麼" in text or "不知道吃啥" in text:
            # 情境 1：隨機推薦菜系
            selected = random.choice(food_categories)
            message = f"🍽️ 今天吃什麼？\n\n🎲 隨機推薦：{selected}"
            line_reply(reply_token, message)
            
        else:
            # 情境 2：地區餐廳搜尋
            # 提取地區（移除「吃什麼」或「吃啥」）
            location = re.sub(r'吃什麼|吃啥', '', text).strip()
            
            if not location:
                # 沒有地區，隨機推薦菜系
                selected = random.choice(food_categories)
                message = f"🍽️ 今天吃什麼？\n\n🎲 隨機推薦：{selected}"
                line_reply(reply_token, message)
                return
            
            app.logger.info(f"📍 搜尋地區：{location}")
            
            # 用 Gemini Search 搜尋餐廳（優化提示詞）
            prompt = f"""搜尋「{location} 餐廳 推薦」，隨機推薦一間在地餐廳。

格式（嚴格遵守，不要任何其他文字）：
餐廳名 | 推薦菜品 | 評分

範例：
大甲阿吉米糕 | 招牌米糕、四神湯 | 4.6

現在開始："""
            
            reply = ask_gemini(prompt, use_search=True)
            
            if not reply:
                # Gemini 失敗，隨機推薦菜系
                app.logger.error(f"Gemini 搜尋失敗")
                selected = random.choice(food_categories)
                message = f"🍽️ {location} 吃什麼？\n\n找不到資料，隨機推薦：{selected}"
                line_reply(reply_token, message)
                return
            
            app.logger.info(f"Gemini 回應: {reply[:200]}")
            
            # 強制清理多餘文字（只保留包含 | 的行）
            lines = reply.split('\n')
            clean_line = None
            for line in lines:
                if '|' in line:
                    clean_line = line.strip()
                    break
            
            if not clean_line:
                # 沒有找到正確格式，隨機推薦菜系
                app.logger.warning("無法解析 Gemini 回應")
                selected = random.choice(food_categories)
                message = f"🍽️ {location} 吃什麼？\n\n找不到資料，隨機推薦：{selected}"
                line_reply(reply_token, message)
                return
            
            # 解析 Gemini 回應（優化：支援 | 分隔格式）
            parts = [p.strip() for p in clean_line.split('|')]
            restaurant_name = parts[0] if len(parts) > 0 else None
            recommend_items = parts[1] if len(parts) > 1 else None
            rating = None
            if len(parts) > 2:
                rating_match = re.search(r'(\d\.\d)', parts[2])
                if rating_match:
                    rating = rating_match.group(1)
            
            if not restaurant_name:
                # 還是提取不到，隨機推薦菜系
                app.logger.warning("無法提取餐廳名稱")
                selected = random.choice(food_categories)
                message = f"🍽️ {location} 吃什麼？\n\n找不到資料，隨機推薦：{selected}"
                line_reply(reply_token, message)
                return
            
            # 組合回應訊息
            message = f"🍽️ {location} 吃什麼？\n\n🎯 推薦：{restaurant_name}"
            
            if rating:
                message += f"\n⭐ 評分：{rating}"
            
            if recommend_items:
                message += f"\n🍴 推薦品項：{recommend_items}"
            
            line_reply(reply_token, message)
    
    except Exception as e:
        app.logger.error(f"handle_what_to_eat 錯誤: {e}")
        import traceback
        app.logger.error(traceback.format_exc())
        line_reply(reply_token, "尋找餐廳時發生錯誤，請稍後再試。")


# ========================
# Webhook
# ========================

def verify_signature(body, signature):
    """驗證 LINE webhook 簽名"""
    import hashlib
    import hmac
    import base64
    hash_val = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256
    ).digest()
    return signature == base64.b64encode(hash_val).decode("utf-8")


@app.route("/line/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    if not verify_signature(body, signature):
        abort(400)

    data = json.loads(body)
    for event in data.get("events", []):
        if event.get("type") == "message" and event.get("message", {}).get("type") == "text":
            threading.Thread(target=process_event, args=(event,), daemon=True).start()

    return "OK", 200


def process_event(event):
    text = event["message"]["text"].strip()
    reply_token = event["replyToken"]
    source = event["source"]
    group_id = source.get("groupId")
    target = group_id or source.get("userId") or "unknown"
    sender_id = source.get("userId", "unknown")
    sender_name = get_display_name(sender_id, group_id)

    app.logger.info(f"訊息: {sender_name} (UID: {sender_id}): {text[:100]}")

    # UberEats 訂單追蹤：同時含 ubereats 和 orders 的 URL
    if 'ubereats' in text.lower() and 'orders' in text.lower():
        import subprocess, os, signal
        uber_url = next((w for w in text.split() if 'ubereats' in w and 'orders' in w), None)
        if uber_url:
            pid_file = '/tmp/ubereats_tracker.pid'
            try:
                if os.path.exists(pid_file):
                    old_pid = int(open(pid_file).read().strip())
                    os.kill(old_pid, signal.SIGTERM)
            except Exception:
                pass
            env = os.environ.copy()
            env['PATH'] = '/home/eeyore/.local/bin:/usr/bin:/bin:' + env.get('PATH', '')
            log_f = open('/tmp/ubereats_tracker.log', 'w')
            proc = subprocess.Popen(
                ['python3', '/home/eeyore/ubereats_tracker.py', uber_url, target, LINE_ACCESS_TOKEN],
                env=env, stdout=log_f, stderr=log_f, start_new_session=True
            )
            with open(pid_file, 'w') as pf:
                pf.write(str(proc.pid))
            line_reply(reply_token, '🍔 UberEats 訂單追蹤已啟動！每 60 秒更新一次狀態')
            return


    # 0. 讚嘆姿儀嬤 → 分享 ATEEZ 熱門影片
    if "讚嘆姿儀嬤" in text:
        threading.Thread(target=handle_ateez, args=(target, reply_token, sender_name), daemon=True).start()
        return

    # 0a. 讚嘆宜庭嬤 → 分享 One Ok Rock 影片
    if "讚嘆宜庭嬤" in text:
        threading.Thread(target=handle_yiting_ama, args=(target, reply_token, sender_name), daemon=True).start()
        return

    # 0b. 讚嘆神豬 → 分享 Twice MV
    if "讚嘆神豬" in text:
        threading.Thread(target=handle_renyou_gong, args=(target, reply_token, sender_name), daemon=True).start()
        return

    # 0b. 姿儀嬤顯靈 → 隨機發送 ATEEZ 圖片
    if "姿儀嬤顯靈" in text:
        threading.Thread(target=handle_ateez_photo, args=(target, reply_token, sender_name), daemon=True).start()
        return

    # 0b-1. 宜庭嬤顯靈 → 隨機發送 One Ok Rock 圖片
    if "宜庭嬤顯靈" in text:
        threading.Thread(target=handle_yiting_ama_photo, args=(target, reply_token, sender_name), daemon=True).start()
        return

    # 0b-2. 神豬顯靈 → 隨機發送 Twice TZUYU 圖片
    if "神豬顯靈" in text:
        threading.Thread(target=handle_shen_zhu_photo, args=(target, reply_token, sender_name), daemon=True).start()
        return

    # 0c. 早安 → 發送早安圖片
    if "早安" in text:
        threading.Thread(target=handle_morning_photo, args=(target, reply_token, sender_name), daemon=True).start()
        return

    # 0d. 晚安 → 發送晚安圖片
    if "晚安" in text:
        threading.Thread(target=handle_goodnight_photo, args=(target, reply_token, sender_name), daemon=True).start()
        return

    # 0c. 生日快樂（精確匹配）→ 有 @ 發圖片，無 @ 發影片 / 包含「生日快樂」或「HappyBirthday」→ 發送圖片
    if text.strip() == "生日快樂":
        if '@' in text:
            # 有 @ 用戶 → 發送圖片
            threading.Thread(target=handle_birthday_image, args=(target, reply_token, sender_name, text), daemon=True).start()
        else:
            # 沒有 @ → 發送影片
            threading.Thread(target=handle_birthday_video, args=(target, reply_token, sender_name, text), daemon=True).start()
        return
    elif "生日快樂" in text or "happybirthday" in text.lower().replace(" ", ""):
        threading.Thread(target=handle_birthday_image, args=(target, reply_token, sender_name, text), daemon=True).start()
        return

    # 0d. 吃什麼 → 隨機推薦菜系或地區餐廳
    # 嚴格規則：
    # 1. 「不知道吃什麼」或「不知道吃啥」→ 隨機推薦菜系
    # 2. 「地點 + 吃什麼」或「地點 + 吃啥」→ 推薦該地區餐廳（地點至少 2 個字）
    if text.strip() == "不知道吃什麼" or text.strip() == "不知道吃啥":
        threading.Thread(target=handle_what_to_eat, args=(target, reply_token, sender_name, text), daemon=True).start()
        return
    
    # 地點 + 吃什麼（地點至少 2 個字，例如「台中大甲 吃什麼」）
    location_eat_pattern = re.match(r'^([\u4e00-\u9fa5a-zA-Z]{2,})\s*(吃什麼|吃啥)\s*$', text.strip())
    if location_eat_pattern:
        threading.Thread(target=handle_what_to_eat, args=(target, reply_token, sender_name, text), daemon=True).start()
        return

    # 0e. 天氣 → 查詢地區天氣 + 未來三天降雨機率
    # 規則：「地點 + 天氣」（地點至少 2 個字，例如「台中苑裡 天氣」）
    location_weather_pattern = re.match(r'^([\u4e00-\u9fa5a-zA-Z]{2,})\s*天氣\s*$', text.strip())
    if location_weather_pattern:
        threading.Thread(target=handle_weather, args=(target, reply_token, sender_name, text), daemon=True).start()
        return


    # 1. 分析XX（台股 + 美股五維分析）
    m = re.match(r'^分析\s*(.+)$', text)
    if m:
        query = m.group(1).strip()
        threading.Thread(target=handle_analyze, args=(target, reply_token, query), daemon=True).start()
        return

    # 2. #推薦 / #選股（每日股市推薦）— 全形半形都吃
    normalized = text.replace("＃", "#").replace("＠", "@")
    if normalized in ("#推薦", "#選股"):
        threading.Thread(target=handle_daily_report, args=(target, reply_token), daemon=True).start()
        return

    # 0. 總 help：>? 或 >？
    if text.strip() in (">?", ">？"):
        line_reply(reply_token, (
            "📖 指令總覽：\n"
            "━━━━━━━━━━━━━━\n"
            "#股票代號 → 即時報價查詢\n"
            "#推薦 → 每日股市推薦\n"
            "分析 股票 → 五維分析\n"
            "%關鍵字 → 搜尋資訊\n"
            "-指令 → 行事曆/待辦/記事本\n"
            "!指令 → 命理/星座/塔羅\n"
            "OO天氣 → 查詢天氣\n"
            "OO吃什麼 → 推薦餐廳\n"
            "elior → AI 對話\n"
            "━━━━━━━━━━━━━━\n"
            "各類詳細說明：\n"
            "  >#  >%  >-  >!  >doc"
        ))
        return

    # 0b. 各類 help：>#  >%  >-  >!  >doc
    if text.strip() in (">#", ">＃"):
        line_reply(reply_token, (
            "📖 # 股票/查詢指令：\n"
            "#台積電 → 即時報價+走勢\n"
            "#AAPL → 美股報價\n"
            "#推薦 → 每日股市推薦\n"
            "#選股 → 同上\n"
            "分析 台積電 → 五維分析報告"
        ))
        return
    if text.strip() in (">%", ">％"):
        line_reply(reply_token, (
            "📖 % 搜尋指令：\n"
            "%今天新聞 → 查新聞\n"
            "%任何問題 → Gemini + Google 搜尋"
        ))
        return
    if text.strip() in (">-", ">－"):
        line_reply(reply_token, (
            "📖 - 指令說明：\n"
            "━━━ 行事曆 ━━━\n"
            "-MMDD 事件名稱 → 新增全天事件\n"
            "-MMDD~MMDD 事件 → 日期範圍事件\n"
            "-MMDD 事件 HH:MM → 新增定時事件\n"
            "-MMDD 事件 顏色 → 指定顏色\n"
            "-MMDD list → 列出該日事件\n"
            "-del MMDD → 刪行事曆\n"
            "-update → 更新事件\n"
            "━━━ 待辦/購物 ━━━\n"
            "-tasks list → 列出待辦+購物清單\n"
            "-買XX → 加入購物清單\n"
            "-XX → 加入待辦事項\n"
            "-del XX → 刪待辦\n"
            "-del 買XX → 刪購物清單\n"
            "━━━ 記事本 ━━━\n"
            ">doc 看詳細說明\n"
            "━━━ 提醒 ━━━\n"
            "-HH:MM 事項 → 設定時間提醒\n"
            "-五點半 事項 → 中文時間提醒"
        ))
        return
    if text.strip() in (">!", ">！"):
        line_reply(reply_token, (
            "📖 ! 命理/占卜指令：\n"
            "!塔羅 → 抽塔羅牌解讀\n"
            "!星座 XX座 → 星座運勢\n"
            "!19900427亥時雲林 → 八字命盤\n"
            "!19900427 21:00 雲林 → 同上\n"
            "!MBTI INFP → MBTI 分析"
        ))
        return
    if text.strip().lower() == ">doc":
        line_reply(reply_token, (
            "📖 -doc 記事本指令：\n"
            "━━━ 寫入 ━━━\n"
            "-doc #1 內容 → 存入指定記事本\n"
            "-doc #新名稱 內容 → 不存在則新建\n"
            "-doc 內容 → 預設存到 #1\n"
            "━━━ 查看 ━━━\n"
            "-doc list → 列出所有記事本\n"
            "-doc #1 → 顯示連結\n"
            "-link doc #1 → 顯示連結\n"
            "-read doc #1 → 讀取最近內容\n"
            "━━━ 編輯 ━━━\n"
            "-del doc #1 → 刪最後一筆\n"
            "-del doc #1 關鍵字 → 刪含關鍵字\n"
            "-undo doc #1 → 刪最後一筆\n"
            "-clear doc #1 → 清空整份\n"
            "━━━ 說明 ━━━\n"
            "連結自動變超連結\n"
            "#後面可接編號或名稱"
        ))
        return

    # 3. #XX（股票/報價即時查詢 — yfinance + Gemini 補充）
    m = re.match(r'^[#＃]\s*(.+)$', text)
    if m:
        query = m.group(1).strip()
        threading.Thread(target=handle_hashtag, args=(target, reply_token, sender_name, query), daemon=True).start()
        return

    # 4. %XX（一般資訊查詢 — 直接 Gemini + Google Search）
    m = re.match(r'^[%％]\s*(.+)$', text)
    if m:
        query = m.group(1).strip()
        threading.Thread(target=handle_at_query, args=(target, reply_token, sender_name, query), daemon=True).start()
        return

    # 5. -XX（Google Workspace：行事曆、購物清單、備忘錄）— 全形半形都吃
    m = re.match(r'^[-－]\s*(.+)$', text, re.DOTALL)
    if m:
        query = m.group(1).strip()
        threading.Thread(target=handle_dash, args=(target, reply_token, sender_name, query), daemon=True).start()
        return

    # 6. !XX（命理、星座、占卜）— 全形半形都吃
    m = re.match(r'^[!！]\s*(.+)$', text)
    if m:
        query = m.group(1).strip()
        # 純符號（如 !? !!? !!!）→ 忽略不處理
        if re.match(r'^[!！?？\s]+$', query):
            return
        # 6a. !擲筊 / !擲茭 → 隨機送一張筊的圖片
        if query in ("擲筊", "擲茭"):
            import random as _rnd
            jiaobei_options = [
                ("聖筊 ✅", "https://files.catbox.moe/swngef.png"),
                ("笑筊 😄", "https://files.catbox.moe/1c73xx.png"),
                ("陰筊 ❌", "https://files.catbox.moe/68ezfg.png"),
            ]
            result, img_url = _rnd.choice(jiaobei_options)
            msgs = [
                {"type": "image", "originalContentUrl": img_url, "previewImageUrl": img_url},
                {"type": "text", "text": result}
            ]
            requests.post(
                "https://api.line.me/v2/bot/message/reply",
                headers={"Authorization": f"Bearer {LINE_ACCESS_TOKEN}", "Content-Type": "application/json"},
                json={"replyToken": reply_token, "messages": msgs},
                timeout=10
            )
            return
        threading.Thread(target=handle_fortune, args=(target, reply_token, sender_name, query), daemon=True).start()
        return

    # 7. Elior（Gemini + Google Search）
    if re.search(r'elior', text, re.IGNORECASE):
        app.logger.info(f"偵測到 Elior！target: {target}, sender: {sender_name}")
        threading.Thread(target=handle_elior, args=(target, reply_token, sender_name, text), daemon=True).start()
        return


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"LINE Bot Dispatcher (Elior) 啟動，port {port}")
    print("功能：elior | 分析XX | #股票 | @搜尋 | !命理 | -記事")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
# 測試註解 - Mon May 11 09:17:02 CST 2026
