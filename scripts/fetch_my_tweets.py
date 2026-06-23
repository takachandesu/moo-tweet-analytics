#!/usr/bin/env python3
"""
fetch_my_tweets.py  (v3 / timeline + 診断つき)

@moo_stock の過去ツイートと指標を twitterapi.io の /twitter/user/last_tweets から
取得し、data/tweet_history.json に upsert する。

v3 の変更点:
- レスポンス構造の揺れ（tweets が直下 or data.tweets の下 等）に対応。
- 1ページ目で HTTPステータス・トップレベルのキー・件数をログ出力（0件時は本文を一部表示）。
- 起動時にバージョン名を表示（どのスクリプトが動いたか一目で分かるように）。

環境変数:
  TWITTERAPI_IO_KEY  必須。twitterapi.io のAPIキー
  X_USERNAME         既定 moo_stock（@なし）
  FETCH_WINDOW_DAYS  既定 30
"""

import os
import sys
import json
import time
from datetime import datetime, timezone, timedelta

import requests

BASE = "https://api.twitterapi.io"
TIMELINE = f"{BASE}/twitter/user/last_tweets"
OUT_PATH = os.environ.get("OUT_PATH", "data/tweet_history.json")
COST_PER_TWEET = 0.00015
MAX_PAGES = 300


def env(name, default=None, required=False):
    val = os.environ.get(name, default)
    if required and not val:
        sys.exit(f"[ERROR] 環境変数 {name} が設定されていません。")
    return val


def pick(d, *keys, default=0):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    pm = d.get("public_metrics") or {}
    for k in keys:
        if k in pm and pm[k] is not None:
            return pm[k]
    return default


def parse_created(s):
    """createdAt 例 'Fri Jun 19 23:59:50 +0000 2026'。ISO形式にも一応対応。"""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y")
    except Exception:
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def extract(body):
    """レスポンスから (tweets, has_next, next_cursor) を構造の違いを吸収して取り出す。"""
    if isinstance(body.get("tweets"), list):
        return body["tweets"], body.get("has_next_page"), body.get("next_cursor")
    data = body.get("data")
    if isinstance(data, dict):
        tweets = data.get("tweets")
        if not isinstance(tweets, list):
            tweets = data.get("tweet_list") if isinstance(data.get("tweet_list"), list) else []
        hn = body.get("has_next_page", data.get("has_next_page"))
        nc = body.get("next_cursor", data.get("next_cursor"))
        return tweets, hn, nc
    if isinstance(data, list):
        return data, body.get("has_next_page"), body.get("next_cursor")
    return [], False, None


def fetch_timeline(api_key, username, cutoff):
    headers = {"X-API-Key": api_key}
    params = {"userName": username}
    collected = []
    cursor = None

    for page in range(MAX_PAGES):
        if cursor:
            params["cursor"] = cursor

        r = None
        for attempt in range(3):
            try:
                r = requests.get(TIMELINE, headers=headers, params=params, timeout=30)
                r.raise_for_status()
                break
            except requests.RequestException as e:
                wait = 2 ** attempt
                print(f"  [WARN] リクエスト失敗 ({e}); {wait}s 後にリトライ")
                time.sleep(wait)
        if r is None:
            print("  [ERROR] リトライ上限。取得を中断。")
            break

        try:
            body = r.json()
        except Exception:
            print(f"  [ERROR] JSONとして読めない応答: {r.text[:300]}")
            break

        tweets, has_next, next_cursor = extract(body)

        if page == 0:
            print(f"  [DEBUG] HTTP {r.status_code} / トップレベルkeys={list(body.keys())} / "
                  f"抽出ツイート数={len(tweets)}")
            if not tweets:
                print(f"  [DEBUG] 応答本文(先頭500字): {json.dumps(body, ensure_ascii=False)[:500]}")

        if not tweets:
            break
        collected.extend(tweets)

        oldest = parse_created(tweets[-1].get("createdAt") or tweets[-1].get("created_at"))
        print(f"  page {page + 1}: +{len(tweets)} 件 (累計 {len(collected)})"
              + (f" / 最古 {oldest.date()}" if oldest else ""))

        if oldest and oldest < cutoff:
            break
        if not has_next:
            break
        if not next_cursor:
            break
        cursor = next_cursor
        time.sleep(0.4)

    return collected


def normalize(t):
    return {
        "id": str(t.get("id")),
        "created_at": pick(t, "createdAt", "created_at", default=""),
        "text": t.get("text", ""),
        "is_reply": bool(t.get("isReply", False)),
        "likes": pick(t, "likeCount", "like_count"),
        "retweets": pick(t, "retweetCount", "retweet_count"),
        "replies": pick(t, "replyCount", "reply_count"),
        "quotes": pick(t, "quoteCount", "quote_count"),
        "bookmarks": pick(t, "bookmarkCount", "bookmark_count"),
        "views": pick(t, "viewCount", "impression_count"),
    }


def load_history(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"username": None, "last_run": None, "tweets": {}}


def main():
    print("[INFO] fetch_my_tweets v3 (timeline + diagnostics) を実行します")
    api_key = env("TWITTERAPI_IO_KEY", required=True)
    username = env("X_USERNAME", "moo_stock").lstrip("@")
    window_days = int(env("FETCH_WINDOW_DAYS", "30"))

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    cutoff = now - timedelta(days=window_days)

    print(f"[INFO] @{username} のタイムラインを {window_days} 日ぶん遡って取得します")
    raw = fetch_timeline(api_key, username, cutoff)
    print(f"[INFO] 取得 {len(raw)} 件 / 概算コスト ${len(raw) * COST_PER_TWEET:.4f}")

    history = load_history(OUT_PATH)
    history["username"] = username
    store = history.setdefault("tweets", {})

    new_count = 0
    kept = 0
    for t in raw:
        rec = normalize(t)
        tid = rec["id"]
        if not tid or tid == "None" or rec["is_reply"]:
            continue
        dt = parse_created(rec["created_at"])
        if dt and dt < cutoff:
            continue
        kept += 1
        if tid in store:
            first_seen = store[tid].get("first_seen", now_iso)
            store[tid].update(rec)
            store[tid]["first_seen"] = first_seen
            store[tid]["last_fetched"] = now_iso
        else:
            rec["first_seen"] = now_iso
            rec["last_fetched"] = now_iso
            store[tid] = rec
            new_count += 1

    history["last_run"] = now_iso
    os.makedirs(os.path.dirname(OUT_PATH) or ".", exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

    print(f"[INFO] 期間内の対象（リプライ/RT除く）: {kept} 件")
    print(f"[DONE] 新規 {new_count} 件 / 累計 {len(store)} 件 を {OUT_PATH} に保存しました")


if __name__ == "__main__":
    main()
