#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LINE Bot 圖片庫自動更新器
支援:
1. 海綿寶寶生日快樂梗圖
2. ATEEZ 姿儀嬤顯靈圖片
"""

import os
import re
import time
import random
import requests
import base64
from datetime import datetime
from pathlib import Path

# ===================== 設定 =====================
GEMINI_API_KEY = "AIzaSyA97Mhy4KU_YZ7pI1u9fP9fyY88TOmi17I"
GEMINI_MODEL = "gemini-2.5-flash"

TARGET_COUNT = 40  # 每個相簿的目標數量
REFILL_THRESHOLD = 30  # 低於此數量時觸發補充
REFILL_AMOUNT = 20  # 每次補充的數量

LIBRARIES = {
    "birthday": {
        "file": Path.home() / "birthday_photos.md",
        "search_query": "SpongeBob happy birthday meme",
        "gemini_prompt": "這張圖片是否包含海綿寶寶(SpongeBob)角色或生日快樂相關元素?只要是卡通風格的生日祝福圖片就回答 yes,否則回答 no。"
    },
    "ateez": {
        "file": Path.home() / "ateez_photos.md",
        "search_query": "ATEEZ kpop group members",
        "gemini_prompt": "這張圖片是否包含 ATEEZ 男團成員(韓國男子音樂組合)?如果是 ATEEZ 成員的照片(個人或團體)就回答 yes,否則回答 no。"
    },
    "oneokrock": {
        "file": Path.home() / "one_ok_rock_photos.md",
        "search_query": "ONE OK ROCK band members japan",
        "gemini_prompt": "這張圖片是否包含 ONE OK ROCK 樂團成員(日本搖滾樂團)?如果是 ONE OK ROCK 成員的照片(個人或團體)就回答 yes,否則回答 no。"
    },
    "tzuyu": {
        "file": Path.home() / "tzuyu_photos.md",
        "search_query": "Tzuyu TWICE kpop",
        "gemini_prompt": "這張圖片是否包含 Twice TZUYU (Tzuyu,TWICE 成員)?如果是 Twice TZUYU 的照片就回答 yes,否則回答 no。"
    },
    "morning": {
        "file": Path.home() / "morning_photos.md",
        "search_query": "good morning cute greeting card",
        "gemini_prompt": "這張圖片是否適合用來傳送早安問候?包含早安文字、陽光、可愛動物、溫馨場景等早晨祝福元素就回答 yes,否則回答 no。"
    },
    "goodnight": {
        "file": Path.home() / "goodnight_photos.md",
        "search_query": "good night sweet dreams greeting card",
        "gemini_prompt": "這張圖片是否適合用來傳送晚安問候?包含晚安文字、月亮星星、溫馨夜景、可愛動物睡覺等晚安祝福元素就回答 yes,否則回答 no。"
    }
}

# ===================== Gemini Vision API =====================
def check_images_batch_with_gemini(urls_with_data, prompt):
    """批次用 Gemini Vision API 驗證多張圖片"""
    try:
        api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"

        # 建立批次 prompt
        parts = [{"text": f"{prompt}\n\n請對以下 {len(urls_with_data)} 張圖片逐一回答 yes 或 no(每行一個答案):"}]

        for i, (url, image_data) in enumerate(urls_with_data, 1):
            parts.append({"text": f"\n圖片 {i}:"})
            parts.append({"inlineData": {"mimeType": "image/jpeg", "data": image_data}})

        payload = {
            "contents": [{
                "parts": parts
            }]
        }

        resp = requests.post(api_url, json=payload, timeout=180)
        if resp.status_code != 200:
            print(f"❌ Gemini API 錯誤: {resp.status_code}")
            return [False] * len(urls_with_data)

        data = resp.json()
        text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "").lower()

        # 解析回答(每行一個 yes/no)
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        results = []
        for line in lines:
            results.append("yes" in line.lower())

        # 如果回答數量不符,全部標記為 False
        if len(results) != len(urls_with_data):
            print(f"⚠️ Gemini 回答數量不符(預期 {len(urls_with_data)},實際 {len(results)})")
            return [False] * len(urls_with_data)

        return results

    except Exception as e:
        print(f"❌ 批次驗證失敗: {e}")
        return [False] * len(urls_with_data)

def check_image_with_gemini(image_url, prompt):
    """用 Gemini Vision API 驗證單張圖片(向後相容)"""
    try:
        # 下載圖片
        response = requests.get(image_url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        if response.status_code != 200:
            return False

        # 轉 base64
        image_data = base64.b64encode(response.content).decode('utf-8')

        # 使用批次函式(單張)
        results = check_images_batch_with_gemini([(image_url, image_data)], prompt)
        return results[0] if results else False

    except Exception as e:
        print(f"❌ 驗證圖片失敗: {e}")
        return False

# ===================== URL 有效性檢查 =====================
def check_url_valid(url):
    """檢查 URL 是否有效且為圖片"""
    try:
        resp = requests.head(url, timeout=2, allow_redirects=True, headers={'User-Agent': 'Mozilla/5.0'})
        if resp.status_code != 200:
            return False

        content_type = resp.headers.get('Content-Type', '').lower()
        return 'image' in content_type

    except:
        return False

def is_line_compatible(url, response=None):
    """檢查圖片是否符合 LINE API 要求（節省除錯時間的關鍵！）"""
    try:
        # 1. 必須是 HTTPS
        if not url.startswith("https://"):
            return False, "不是 HTTPS"
        
        # 2. 如果已有 response，直接檢查；否則傳送 HEAD 請求
        if response is None:
            resp = requests.head(url, timeout=5, allow_redirects=True, headers={
                "User-Agent": "Mozilla/5.0 (compatible; LINE/1.0)"
            })
        else:
            resp = response
        
        content_type = resp.headers.get("content-type", "").lower()
        content_length = resp.headers.get("content-length", "0")
        
        # 3. Content-Type 必須是 LINE 接受的圖片格式
        valid_types = ["image/jpeg", "image/png", "image/gif", "image/webp"]
        if not any(t in content_type for t in valid_types):
            return False, f"不支援的型別: {content_type}"
        
        # 4. 大小限制 <10MB
        size_mb = int(content_length) / (1024 * 1024) if content_length.isdigit() else 0
        if size_mb > 10:
            return False, f"檔案太大: {size_mb:.1f}MB"
        
        return True, f"✓ {content_type}"
        
    except Exception as e:
        return False, f"檢查失敗: {str(e)[:30]}"

# ===================== SearxNG 搜尋 =====================
def fetch_from_searxng(query, count=50):
    """用 SearxNG 元搜尋引擎搜尋圖片"""
    searxng_instances = [
        "http://localhost:8888",  # 本地例項 - 優先
        "https://searx.be",
        "https://search.im-in.space"
    ]

    batch_urls = []

    for instance in searxng_instances:
        try:
            print(f"  🔍 嘗試例項: {instance}")
            api_url = f"{instance}/search"
            params = {
                'q': query,
                'categories': 'images',
                'format': 'json',
                'pageno': 1,
                'language': 'en'
            }

            verify_ssl = False if 'localhost' in instance else True
            response = requests.get(api_url, params=params, timeout=15, verify=verify_ssl)

            if response.status_code != 200:
                print(f"    ❌ 失敗 ({response.status_code})")
                continue

            data = response.json()
            results = data.get('results', [])

            print(f"    ✅ 找到 {len(results)} 個 URL")

            # 提取圖片 URL
            for result in results[:count * 2]:
                img_url = result.get('img_src') or result.get('thumbnail_src') or result.get('url')
                if not img_url:
                    continue

                # 優先選擇穩定的圖床和直接圖片 URL
                if any(domain in img_url for domain in ['imgur.com', 'i.redd.it', 'i.pinimg.com', 'pbs.twimg.com', 'tenor.com', 'giphy.com']):
                    batch_urls.append(img_url)
                elif any(img_url.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp']):
                    batch_urls.append(img_url)

            # 如果本地例項成功,就停止
            if 'localhost' in instance and len(batch_urls) > 0:
                print("  🎯 本地例項成功,停止嘗試其他例項")
                break

        except Exception as e:
            print(f"    ❌ 錯誤: {e}")
            continue

    # 去重
    urls = list(set(batch_urls))
    print(f"✅ SearxNG 總共找到 {len(urls)} 張圖片")
    return urls

# ===================== 更新單個相簿 =====================
def update_library(lib_name, config):
    """更新單個圖片庫"""
    photo_file = config["file"]
    search_query = config["search_query"]
    gemini_prompt = config["gemini_prompt"]

    print("\n" + "=" * 60)
    print(f"📚 更新相簿: {lib_name}")
    print(f"執行時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # 讀取現有圖片
    all_urls = set()
    available_urls = set()

    if photo_file.exists():
        with open(photo_file, 'r', encoding='utf-8') as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith('#'):
                    url = stripped.lstrip('# ').strip()
                    if url.startswith('http'):
                        all_urls.add(url)
                elif stripped.startswith('http'):
                    all_urls.add(stripped)
                    available_urls.add(stripped)

        print(f"📋 現有圖片: {len(all_urls)} 張")
        print(f"📊 可用圖片: {len(available_urls)} 張(未傳送)")
        print(f"✅ 已傳送: {len(all_urls) - len(available_urls)} 張")

    # 需要新增的數量（低於 30 張就補充 20 張）
    # 需要新增的數量（使用全域性設定）
    
    if len(available_urls) >= REFILL_THRESHOLD:
        print(f"✅ 已有足夠圖片 ({len(available_urls)} >= {REFILL_THRESHOLD}),無需更新")
        return
    
    needed = REFILL_AMOUNT
    print(f"🎯 目標新增: {needed} 張(目前 {len(available_urls)} 張,低於 {REFILL_THRESHOLD} 張門檻)")
    print()

    # 搜尋候選圖片
    print(f"📡 用 SearxNG 搜尋圖片: {search_query}")
    candidate_urls = fetch_from_searxng(query=search_query, count=needed + 20)

    # 去重
    candidate_urls = list(set(candidate_urls) - all_urls)
    random.shuffle(candidate_urls)

    print(f"\n📊 候選圖片: {len(candidate_urls)} 張")
    print()
    
    # 批次驗證（每批 20 張）— 跳過 URL 檢查，直接下載+驗證
    BATCH_SIZE = 20
    validated_urls = []
    
    print(f"🔍 批次下載並用 Gemini Vision 驗證...")
    
    for batch_start in range(0, min(len(candidate_urls), needed + 100), BATCH_SIZE):
        if len(validated_urls) >= needed:
            break
        
        batch_urls = candidate_urls[batch_start:batch_start + BATCH_SIZE]
        print(f"\n📦 批次 {batch_start // BATCH_SIZE + 1}: 處理 {len(batch_urls)} 張圖片...")
        
        # 直接下載圖片並檢查 LINE 相容性
        urls_with_data = []
        for url in batch_urls:
            try:
                response = requests.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
                if response.status_code == 200:
                    # ⭐ 關鍵：檢查 LINE API 相容性（避免花一小時除錯！）
                    is_valid, reason = is_line_compatible(url, response)
                    if is_valid:
                        image_data = base64.b64encode(response.content).decode('utf-8')
                        urls_with_data.append((url, image_data))
                    # else:
                    #     print(f"  ⚠️ 跳過（{reason}）: {url[:50]}")
            except:
                pass  # 靜默跳過失敗的 URL
        
        if not urls_with_data:
            print("  ⚠️ 批次下載全部失敗，跳過")
            continue
        
        print(f"  ✅ 成功下載 {len(urls_with_data)}/{len(batch_urls)} 張")
        print(f"  🤖 呼叫 Gemini Vision API 驗證...")
        
        # 批次驗證
        results = check_images_batch_with_gemini(urls_with_data, gemini_prompt)
        
        # 統計結果
        for (url, _), passed in zip(urls_with_data, results):
            if passed:
                validated_urls.append(url)
                print(f"  ✅ 透過: {url[:50]}...")
        
        print(f"  📊 批次結果：{sum(results)}/{len(results)} 張透過")
        print(f"  🎯 累計已驗證透過: {len(validated_urls)} 張")
        
        if len(validated_urls) >= needed:
            print(f"\n✅ 已達成目標！")
            break
        
        # 批次間的延遲
        if batch_start + BATCH_SIZE < len(candidate_urls):
            time.sleep(2)

    # 寫入檔案
    if validated_urls:
        with open(photo_file, 'a', encoding='utf-8') as f:
            for url in validated_urls:
                f.write(f"{url}\n")

        print()
        print("=" * 60)
        print(f"✅ 成功新增 {len(validated_urls)} 張圖片")
        print(f"📁 檔案位置: {photo_file}")
        print(f"📊 可用圖片: {len(available_urls) + len(validated_urls)} 張")
        print(f"📚 總圖片數: {len(all_urls) + len(validated_urls)} 張")
        print("=" * 60)
    else:
        print()
        print("⚠️  未找到符合條件的新圖片")

# ===================== 主流程 =====================
def main():
    import sys

    # 如果指定相簿名稱,只更新該相簿
    if len(sys.argv) > 1:
        lib_name = sys.argv[1]
        if lib_name in LIBRARIES:
            update_library(lib_name, LIBRARIES[lib_name])
        else:
            print(f"❌ 未知的相簿: {lib_name}")
            print(f"可用相簿: {', '.join(LIBRARIES.keys())}")
    else:
        # 更新所有相簿
        for lib_name, config in LIBRARIES.items():
            update_library(lib_name, config)
            time.sleep(2)  # 避免 API rate limit

if __name__ == "__main__":
    main()
