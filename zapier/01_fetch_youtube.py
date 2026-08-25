
# Runs inside a Zapier "Code by Zapier" (Python) step — not standalone.
# Reads input_data: api_key, channels (CSV string), videos_per_channel.
# Returns `output` as a list of dicts; Zapier fans out one run per video.

import time

import requests

BASE = "https://www.googleapis.com/youtube/v3"

api_key = input_data["api_key"]
channels = [c.strip() for c in input_data["channels"].split(",") if c.strip()]
per_channel = int(input_data.get("videos_per_channel", 5))

# Code by Zapier caps runtime at 30s on Professional. Each channel costs
# roughly two sequential API calls, so keep the roster small per run.
# For a larger roster, split channels across several scheduled Zaps.
MAX_CHANNELS = 10
DEDUPE = True   # flip to True once the Zap runs cleanly
channels = channels[:MAX_CHANNELS]


def api_get(endpoint, params, retries=2):
    params = {**params, "key": api_key}
    for attempt in range(retries):
        try:
            resp = requests.get(f"{BASE}/{endpoint}", params=params, timeout=8)
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(1)
            continue

        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 403:
            # Quota exhausted or restricted key — retrying cannot help.
            msg = resp.json().get("error", {}).get("message", "")
            raise Exception("YouTube 403: " + msg)
        if resp.status_code in (429, 500, 502, 503) and attempt < retries - 1:
            time.sleep(1)
            continue
        raise Exception("YouTube HTTP %s: %s" % (resp.status_code, resp.text[:200]))
    raise Exception("YouTube: retries exhausted for " + endpoint)


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# --- Step 1: resolve channels -> uploads playlist ---------------------------
handles = [c for c in channels if c.startswith("@")]
ids = [c for c in channels if not c.startswith("@")]
raw_channels = []

for handle in handles:
    data = api_get("channels", {
        "part": "snippet,statistics,contentDetails",
        "forHandle": handle,
    })
    raw_channels.extend(data.get("items", []))

for batch in chunked(ids, 50):
    data = api_get("channels", {
        "part": "snippet,statistics,contentDetails",
        "id": ",".join(batch),
    })
    raw_channels.extend(data.get("items", []))

channel_meta = {}
for c in raw_channels:
    channel_meta[c["id"]] = {
        "channel_title": c["snippet"]["title"],
        "uploads": c["contentDetails"]["relatedPlaylists"]["uploads"],
        "subscribers": int(c["statistics"].get("subscriberCount", 0)),
    }

# --- Step 2: recent video IDs ----------------------------------------------
video_ids = []
for cid, meta in channel_meta.items():
    try:
        data = api_get("playlistItems", {
            "part": "contentDetails",
            "playlistId": meta["uploads"],
            "maxResults": min(per_channel, 50),
        })
        video_ids.extend(
            item["contentDetails"]["videoId"] for item in data.get("items", [])
        )
    except Exception as exc:
        # One bad channel must not kill the run. This gets surfaced in the
        # Zap history logs rather than silently swallowed.
        print("skipping %s: %s" % (meta["channel_title"], exc))

# --- Dedupe against previously seen IDs -------------------------------------
# StoreClient is Zapier's built-in key-value store, scoped to your account.
# Doing the dedupe here means only genuinely new videos leave this step, so
# downstream steps do not burn a task per already-logged video.
# Wrapped in try/except so the Zap still works if the store is unavailable.
seen = []
store = None
store_ok = False
if DEDUPE:
    try:
        try:
            store = StoreClient()          # noqa: F821
        except TypeError:
            store = StoreClient("creator-tracker-v1")  # noqa: F821
        seen = store.get("seen_video_ids") or []
        store_ok = True
    except Exception as exc:
        print("StoreClient unavailable: %s" % exc)
new_ids = [v for v in video_ids if v not in seen] if DEDUPE else video_ids

# --- Step 3: stats for the new videos only ----------------------------------
rows = []
for batch in chunked(new_ids, 50):
    data = api_get("videos", {
        "part": "snippet,statistics",
        "id": ",".join(batch),
    })
    for v in data.get("items", []):
        s = v.get("statistics", {})
        sn = v["snippet"]
        # likeCount / commentCount disappear when a creator disables them.
        views = int(s.get("viewCount", 0))
        likes = int(s.get("likeCount", 0))
        comments = int(s.get("commentCount", 0))
        rows.append({
            "video_id": v["id"],
            "channel_id": sn["channelId"],
            "channel_title": channel_meta.get(sn["channelId"], {}).get(
                "channel_title", "unknown"
            ),
            "title": sn["title"],
            # Truncated: the AI step only needs enough to spot brand mentions
            # and disclosure language, and long text inflates token cost.
            "description": sn.get("description", "")[:1000],
            "published_at": sn["publishedAt"],
            "url": "https://www.youtube.com/watch?v=" + v["id"],
            "views": views,
            "likes": likes,
            "comments": comments,
            "engagements": views + likes + comments,
            "logged_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "subscribers": channel_meta.get(sn["channelId"], {}).get("subscribers", 0),
            "analysis_text": (sn.get("description") or "").strip()[:1000]
                             or sn["title"],
            "text_source": "description" if (sn.get("description") or "").strip()
                           else "title_fallback",
            "text_length": len((sn.get("description") or "").strip()),
        })

# Only record IDs we actually processed successfully, so a mid-run failure
# does not cause videos to be permanently skipped on the next run.
if DEDUPE and store_ok and rows:
    processed = [r["video_id"] for r in rows]
    StoreClient.set("seen_video_ids", (seen + processed)[-150:])  # noqa: F821

print("found %d new videos from %d channels" % (len(rows), len(channel_meta)))

output = rows