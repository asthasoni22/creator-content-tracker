# Standalone script — run locally or in Google Colab. Prompts for an API key.
# Validates the YouTube Data API calls before they get ported into Zapier.
# Writes creator_posts.csv, used to seed the Zapier Posts table.

import time
from getpass import getpass

import pandas as pd
import requests

BASE = "https://www.googleapis.com/youtube/v3"

# Prompt for the key instead of hardcoding it. Same habit you'd use for
# secrets management in a real automation — never commit the key.
API_KEY = getpass("YouTube Data API key: ")

# Your creator roster. Handles (@name) or channel IDs (UC...) both work.
CHANNELS = [
    "@mkbhd",
    "@LinusTechTips",
    "@ThePrimeagen",
    "@fireship_dev",
]

VIDEOS_PER_CHANNEL = 5


# ---------------------------------------------------------------------------
# Low-level request helper — one place for retries, timeouts, and error shape
# ---------------------------------------------------------------------------

def api_get(endpoint, params, retries=3):
    """GET against the YouTube API with retry on transient failures."""
    params = {**params, "key": API_KEY}
    url = f"{BASE}/{endpoint}"

    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=15)
        except requests.RequestException as exc:
            if attempt == retries - 1:
                raise RuntimeError(f"{endpoint}: network error — {exc}") from exc
            time.sleep(2 ** attempt)
            continue

        if resp.status_code == 200:
            return resp.json()

        # 403 is usually quota exhaustion or a key restriction. Retrying
        # won't help, so fail loudly rather than burning attempts.
        if resp.status_code == 403:
            reason = resp.json().get("error", {}).get("message", "")
            raise RuntimeError(f"{endpoint}: 403 forbidden — {reason}")

        # 5xx and 429 are worth backing off on.
        if resp.status_code in (429, 500, 502, 503) and attempt < retries - 1:
            time.sleep(2 ** attempt)
            continue

        raise RuntimeError(f"{endpoint}: HTTP {resp.status_code} — {resp.text[:200]}")

    raise RuntimeError(f"{endpoint}: exhausted retries")


def chunked(seq, size):
    """Yield successive chunks — the API caps id lists at 50."""
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# ---------------------------------------------------------------------------
# Step 1 — channels.list: metadata + the uploads playlist ID
# ---------------------------------------------------------------------------

def fetch_channels(identifiers):
    """
    Accepts a mix of @handles and UC... IDs.

    Handles must be looked up one at a time (forHandle takes a single value),
    but raw IDs can be batched 50 at a time. Returns a list of channel dicts.
    """
    handles = [c for c in identifiers if c.startswith("@")]
    ids = [c for c in identifiers if not c.startswith("@")]
    out = []

    for handle in handles:
        data = api_get("channels", {
            "part": "snippet,statistics,contentDetails",
            "forHandle": handle,
        })
        items = data.get("items", [])
        if not items:
            print(f"  ! no channel found for {handle} — skipping")
            continue
        out.extend(items)

    for batch in chunked(ids, 50):
        data = api_get("channels", {
            "part": "snippet,statistics,contentDetails",
            "id": ",".join(batch),
        })
        out.extend(data.get("items", []))

    return [
        {
            "channel_id": c["id"],
            "channel_title": c["snippet"]["title"],
            "subscribers": int(c["statistics"].get("subscriberCount", 0)),
            "uploads_playlist": c["contentDetails"]["relatedPlaylists"]["uploads"],
        }
        for c in out
    ]


# ---------------------------------------------------------------------------
# Step 2 — playlistItems.list: which videos exist (no stats here)
# ---------------------------------------------------------------------------

def fetch_recent_video_ids(uploads_playlist, limit=5):
    data = api_get("playlistItems", {
        "part": "contentDetails",
        "playlistId": uploads_playlist,
        "maxResults": min(limit, 50),
    })
    return [item["contentDetails"]["videoId"] for item in data.get("items", [])]


# ---------------------------------------------------------------------------
# Step 3 — videos.list: the actual engagement numbers
# ---------------------------------------------------------------------------

def fetch_video_stats(video_ids):
    """Batch up to 50 video IDs per call."""
    rows = []
    for batch in chunked(video_ids, 50):
        data = api_get("videos", {
            "part": "snippet,statistics",
            "id": ",".join(batch),
        })
        for v in data.get("items", []):
            stats = v.get("statistics", {})
            snippet = v["snippet"]
            rows.append({
                "video_id": v["id"],
                "channel_id": snippet["channelId"],
                "title": snippet["title"],
                "description": snippet.get("description", "")[:500],
                "published_at": snippet["publishedAt"],
                "url": f"https://www.youtube.com/watch?v={v['id']}",
                # likeCount and commentCount disappear when the creator
                # disables them — always use .get() with a default.
                "views": int(stats.get("viewCount", 0)),
                "likes": int(stats.get("likeCount", 0)),
                "comments": int(stats.get("commentCount", 0)),
            })
    return rows


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def build_dataset(identifiers, videos_per_channel=5):
    print(f"Resolving {len(identifiers)} channels...")
    channels = fetch_channels(identifiers)
    print(f"  resolved {len(channels)}")

    by_id = {c["channel_id"]: c for c in channels}
    all_video_ids = []

    for ch in channels:
        try:
            ids = fetch_recent_video_ids(ch["uploads_playlist"], videos_per_channel)
            all_video_ids.extend(ids)
            print(f"  {ch['channel_title']}: {len(ids)} videos")
        except RuntimeError as exc:
            # One bad channel shouldn't kill the whole run — this is the
            # error-isolation behaviour you'll want in the Zap too.
            print(f"  ! {ch['channel_title']}: {exc}")

    print(f"Fetching stats for {len(all_video_ids)} videos...")
    rows = fetch_video_stats(all_video_ids)

    for r in rows:
        ch = by_id.get(r["channel_id"], {})
        r["channel_title"] = ch.get("channel_title", "unknown")
        r["subscribers"] = ch.get("subscribers", 0)
        r["engagements"] = r["views"] + r["likes"] + r["comments"]

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("published_at", ascending=False).reset_index(drop=True)
    return df


if __name__ == "__main__":
    df = build_dataset(CHANNELS, VIDEOS_PER_CHANNEL)

    print(f"\n{len(df)} rows\n")
    print(df[["channel_title", "title", "views", "likes", "comments"]].head(20))

    # Cost-per-engagement, the ROI metric from the job description.
    # Swap the flat rate for a real per-creator payout from your Creators table.
    PAYOUT_RATE = 500.0
    if not df.empty:
        df["cost_per_engagement"] = (PAYOUT_RATE / df["engagements"]).round(4)
        print("\nCost per engagement:")
        print(df[["channel_title", "title", "engagements", "cost_per_engagement"]].head(10))

    df.to_csv("creator_posts.csv", index=False)
    print("\nWrote creator_posts.csv")