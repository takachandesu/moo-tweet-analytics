#!/usr/bin/env python3
"""
fetch_my_tweets.py

@moo_stock の過去ツイートとエンゲージメント指標を twitterapi.io から取得し、
data/tweet_history.json に蓄積（upsert）するスクリプト。

取得方式:
- /twitter/user/last_tweets（ユーザーのタイムラインを created_at 降順で辿る）を使用。
  検索(advanced_search)は Twitter 側で since:/until: が無効化され古い分を遡れないため、
  タイムライン走査に切り替えている。1ページ20件、cursor でページング。
- 新しい順に取得し、FETCH_WINDOW_DAYS より古いツイートに達したら停止。
- 既存ツイートは指標を「更新」する（いいね/RT等は投稿後しばらく伸びるため）。
- リプライ・リツイートは除外し、オリジナル投稿（ニュース投稿・ランキング等）を対象。

必要な環境変数:
  TWITTERAPI_IO_KEY  twitterapi.io のダッシュボードで発行した API キー
  X_USERNAME         自分の X ユーザー名（@ なし）。既定 moo_stock
  FETCH_WINDOW_DAYS  遡及日数（既定 30）
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
COST_PER_TWEET = 0.00015          # USD, 参考表示用
MAX_PAGES = 300                   # 安全弁（1ページ20件 → 最大6000件相当）


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


def parse_created(s):
    """createdAt 例: 'Fri Jun 19 23:59:50 +0000 2026' を aware datetime に。"""
    try:
        return datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y")
    except Exception:
        return None


def fetch_timeline(api_key, username, cutoff):
    """タイムラインを新しい順に辿り、cutoff より新しいツイートを集めて返す。"""
    headers = {"X-API-Key": api_key}
    params = {"userName": username}
    collected = []
    cursor = None

    for page in range(MAX_PAGES):
        if cursor:
            params["cursor"] = cursor
        for attempt in range(3):
            try:
                r = requests.get(TIMELINE, headers=headers, params=params, timeout=30)
                r.raise_for_status()
                break
            except requests.RequestException as e:
                wait = 2 ** attempt
                print(f"  [WARN] リクエスト失敗 ({e}); {wait}s 後にリトライ")
                time.sleep(wait)
        else:
            print("  [ERROR] リトライ上限。取得を中断。")
            break

        body = r.json()
        tweets = body.get("tweets", [])
        if not tweets:                       # 空ページ＝終端（has_next_pageの偽陽性対策）
            break
        collected.extend(tweets)

        oldest = parse_created(tweets[-1].get("createdAt", ""))
        print(f"  page {page + 1}: +{len(tweets)} 件 (累計 {len(collected)})"
              + (f" / 最古 {oldest.date()}" if oldest else ""))
        if oldest and oldest < cutoff:       # 期間外まで遡ったら終了
            break
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
        if dt and dt < cutoff:               # 期間外は保存しない
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
