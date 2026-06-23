#!/usr/bin/env python3
"""
fetch_organic_metrics.py

公式 X API v2 で「自分のツイート」の organic_metrics を取得し、
data/tweet_history.json の各レコードに以下を付与する:
  - organic_impressions : オーナー側のインプレッション
  - link_clicks         : url_link_clicks（本文リンクのクリック数）
  - profile_clicks      : user_profile_clicks（プロフィールへの遷移数）★CTAの成果指標

制約（X API 仕様）:
  - 取れるのは自分（認証アカウント）のツイートのみ。
  - 投稿から30日以内のツイートのみ。古いものは organic を返さない。
  - 公式APIなので読み取りも課金対象（Developersのクレジットを消費）。

認証: OAuth1.0a ユーザーコンテキスト（投稿用と同じ4トークン）
  X_API_KEY / X_API_SECRET / X_ACCESS_TOKEN / X_ACCESS_SECRET
"""

import os
import sys
import json
from datetime import datetime, timezone, timedelta

import tweepy

OUT_PATH = os.environ.get("OUT_PATH", "data/tweet_history.json")
WINDOW_DAYS = 28          # 30日制限の安全マージン
BATCH = 100               # /2/tweets は最大100 IDまで


def env(name, required=True):
    v = os.environ.get(name)
    if required and not v:
        sys.exit(f"[ERROR] 環境変数 {name} が設定されていません。")
    return v


def parse_created(s):
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


def main():
    print("[INFO] fetch_organic_metrics: 公式X APIでプロフィール/リンククリックを取得します")

    client = tweepy.Client(
        consumer_key=env("X_API_KEY"),
        consumer_secret=env("X_API_SECRET"),
        access_token=env("X_ACCESS_TOKEN"),
        access_token_secret=env("X_ACCESS_SECRET"),
    )

    if not os.path.exists(OUT_PATH):
        sys.exit(f"[ERROR] {OUT_PATH} がありません。先にツイート取得を実行してください。")

    hist = json.load(open(OUT_PATH, encoding="utf-8"))
    store = hist.get("tweets", {})
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    cutoff = now - timedelta(days=WINDOW_DAYS)

    ids = [tid for tid, t in store.items()
           if (parse_created(t.get("created_at")) or now) >= cutoff]
    print(f"[INFO] 30日以内の対象 {len(ids)} 件を取得します（概算コスト ${len(ids)*0.001:.3f}）")

    enriched = 0
    no_organic = 0
    for i in range(0, len(ids), BATCH):
        batch = ids[i:i + BATCH]
        try:
            resp = client.get_tweets(
                ids=batch,
                user_auth=True,   # OAuth1.0a ユーザーコンテキスト（organic取得に必須）
                tweet_fields=["created_at", "public_metrics",
                              "organic_metrics", "non_public_metrics"],
            )
        except Exception as e:
            print(f"  [WARN] バッチ取得失敗: {e}")
            continue

        if i == 0:
            ndata = len(resp.data) if resp.data else 0
            nerr = len(resp.errors) if getattr(resp, "errors", None) else 0
            print(f"  [DEBUG] 1バッチ目: data={ndata}件 / errors={nerr}件")
            if resp.data:
                t0 = resp.data[0]
                print(f"  [DEBUG] サンプル organic_metrics = {getattr(t0,'organic_metrics',None)}")
                print(f"  [DEBUG] サンプル non_public_metrics = {getattr(t0,'non_public_metrics',None)}")

        for tw in (resp.data or []):
            om = getattr(tw, "organic_metrics", None) or getattr(tw, "non_public_metrics", None)
            rec = store.get(str(tw.id))
            if rec is None:
                continue
            if om:
                rec["organic_impressions"] = om.get("impression_count")
                rec["link_clicks"] = om.get("url_link_clicks")
                rec["profile_clicks"] = om.get("user_profile_clicks")
                rec["metrics_fetched"] = now_iso
                enriched += 1
            else:
                no_organic += 1

    json.dump(hist, open(OUT_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[DONE] organic付与 {enriched} 件 / organic取得できず {no_organic} 件")
    if enriched == 0:
        print("[WARN] organic_metricsが1件も取れていません。"
              "アプリ権限(Read)・認証・30日制限・アクセスtierを確認してください。")


if __name__ == "__main__":
    main()
