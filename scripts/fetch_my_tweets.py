#!/usr/bin/env python3
"""
fetch_my_tweets.py

@moo_stock の過去ツイートとエンゲージメント指標を twitterapi.io から取得し、
data/tweet_history.json に蓄積（upsert）するスクリプト。

設計（高頻度アカウント対応）:
- 取得期間を CHUNK_DAYS 日ごとに区切り、各区間で advanced_search を回す。
  （from:user を1本のcursorで深掘りすると50ページ超で不安定になるため、区間分割で回避）
- 各区間は has_next_page が false になるまで next_cursor でページング。
- 既存ツイートは指標を「更新」する（いいね/RT等は投稿後しばらく伸びるため）。
- リプライ・リツイートは除外し、オリジナル投稿（ニュース投稿・ランキング等）を対象。

必要な環境変数:
  TWITTERAPI_IO_KEY  twitterapi.io のダッシュボードで発行した API キー
  X_USERNAME         自分の X ユーザー名（@ なし）。既定 moo_stock
  FETCH_WINDOW_DAYS  遡及日数（既定 30）
  CHUNK_DAYS         1区間の日数（既定 7）
"""

import os
import sys
import json
import time
from datetime import datetime, timezone, timedelta

import requests

BASE = "https://api.twitterapi.io"
SEARCH = f"{BASE}/twitter/tweet/advanced_search"
OUT_PATH = os.environ.get("OUT_PATH", "data/tweet_history.json")
COST_PER_TWEET = 0.00015          # USD, 参考表示用
MAX_PAGES_PER_CHUNK = 50          # 1区間あたりのページ上限（安全弁）


def env(name, default=None, required=False):
    val = os.environ.get(name, default)
    if required and not val:
        sys.exit(f"[ERROR] 環境変数 {name} が設定されていません。")
    return val


def pick(d, *keys, default=0):
    """camelCase / snake_case / public_metrics のどれで返ってきても拾えるように。"""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    pm = d.get("public_metrics") or {}
    for k in keys:
        if k in pm and pm[k] is not None:
            return pm[k]
    return default


def fetch_chunk(api_key, username, since_date, until_date):
    """from:username の [since_date, until_date) を全ページ取得して返す。"""
    headers = {"X-API-Key": api_key}
    query = (f"from:{username} since:{since_date} until:{until_date} "
             f"-filter:replies -filter:nativeretweets")
    params = {"query": query, "queryType": "Latest"}

    collected = []
    cursor = None
    for page in range(MAX_PAGES_PER_CHUNK):
        if cursor:
            params["cursor"] = cursor
        for attempt in range(3):
            try:
                r = requests.get(SEARCH, headers=headers, params=params, timeout=30)
                r.raise_for_status()
                break
            except requests.RequestException as e:
                wait = 2 ** attempt
                print(f"    [WARN] リクエスト失敗 ({e}); {wait}s 後にリトライ")
                time.sleep(wait)
        else:
            print("    [ERROR] リトライ上限。この区間を中断。")
            break

        body = r.json()
        tweets = body.get("tweets", [])
        collected.extend(tweets)

        if not body.get("has_next_page"):
            break
        cursor = body.get("next_cursor")
        if not cursor:
            break
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
        "views": pick(t, "viewCount", "impression_count"),  # impressions
    }


def load_history(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"username": None, "last_run": None, "tweets": {}}


def main():
    api_key = env("TWITTERAPI_IO_KEY", required=True)
    username = env("X_USERNAME", "moo_stock").lstrip("@")
    window_days = int(env("FETCH_WINDOW_DAYS", "30"))
    chunk_days = int(env("CHUNK_DAYS", "7"))

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    start = now - timedelta(days=window_days)

    history = load_history(OUT_PATH)
    history["username"] = username
    store = history.setdefault("tweets", {})

    print(f"[INFO] @{username} の直近 {window_days} 日を {chunk_days} 日区切りで取得します")

    total_raw = 0
    new_count = 0
    chunk_start = start
    while chunk_start < now:
        chunk_end = min(chunk_start + timedelta(days=chunk_days), now)
        s = chunk_start.strftime("%Y-%m-%d")
        e = chunk_end.strftime("%Y-%m-%d")
        print(f"  区間 {s} 〜 {e} を取得中 ...")
        raw = fetch_chunk(api_key, username, s, e)
        total_raw += len(raw)
        print(f"    取得 {len(raw)} 件")

        for t in raw:
            rec = normalize(t)
            tid = rec["id"]
            if not tid or tid == "None" or rec["is_reply"]:
                continue
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

        chunk_start = chunk_end

    history["last_run"] = now_iso
    os.makedirs(os.path.dirname(OUT_PATH) or ".", exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

    print(f"[INFO] 取得のべ {total_raw} 件 / 概算コスト ${total_raw * COST_PER_TWEET:.4f}")
    print(f"[DONE] 新規 {new_count} 件 / 累計 {len(store)} 件 を {OUT_PATH} に保存しました")


if __name__ == "__main__":
    main()
