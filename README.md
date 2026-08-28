# Creator Content Tracker

An automated pipeline that tracks influencer content on YouTube, joins it against internal campaign data, and computes cost-per-engagement, without anyone opening a spreadsheet.

Built in Zapier with custom Python steps. Weekend project to get hands-on with low-code automation platforms.

---

## The problem

Running a creator marketing program means knowing when creators post, how those posts perform, and what each engagement actually cost. Done manually, someone checks channels, copies numbers into a sheet, and looks up payout rates to work out ROI. It's slow, it goes stale the moment view counts move, and it doesn't scale past a handful of creators.

This automates the whole loop.

---

## Architecture

```
1. Schedule by Zapier      daily trigger
2. Code by Zapier          fetch new videos from YouTube Data API v3
3. Zapier Tables           look up the creator in the roster
4. Filter by Zapier        continue only for active campaigns
5. AI by Zapier            classify brand mention + sponsorship disclosure
6. Code by Zapier          compute cost-per-engagement
7. Zapier Tables           write one row per video
```

Step 2 returns a list, so Zapier fans out — steps 3 through 7 run once per video.

This was originally eight steps. See [What changed](#what-changed-and-why) below.

### Why three API calls

The YouTube Data API splits metadata and statistics across resources:

| Endpoint | Returns | Quota |
|---|---|---|
| `channels.list` | Channel metadata, uploads playlist ID | 1 unit |
| `playlistItems.list` | Recent video IDs (no stats) | 1 unit |
| `videos.list` | View / like / comment counts | 1 unit |

`search.list` looks like the obvious entry point but costs 100 units per call against a 10,000/day allowance. Avoiding it keeps the whole pipeline at roughly 3 units per channel.

Both `channels.list` and `videos.list` accept up to 50 comma-separated IDs, so requests are batched rather than looped per item.

---

## Data model

Two tables, joined on `channel_id`.

**Creators** — the roster. Campaign, payout rate, brand keywords, status. Maintained by hand.

**Posts** — one row per tracked video. Engagement metrics, AI classification, computed ROI.

The split matters: the YouTube API knows views, and only the internal table knows what was paid. Keeping payout rates out of the Posts table means renegotiating a rate doesn't require rewriting historical rows. The join is where ROI becomes computable at all.

### A note on column types

`brand_mentioned` and `disclosure_present` are **text columns, not checkboxes**. This was a bug before it was a decision.

A Zapier Tables checkbox column coerces any non-empty string to `true`. The classifier returns `"no"` — a non-empty string — so every row came back checked. Worse, so did `"pending"`. A boolean column can only represent two states, and this data has three: yes, no, and we-don't-know. Text preserves the third.

---

## What changed, and why

The pipeline shipped at seven steps rather than the eight it was designed with. Two of the changes came out of debugging, and both are more interesting than the original design.

### The AI parse step was deleted

The original step 6 took the raw response from AI by Zapier, extracted the JSON, and mapped the values into `brand_mentioned`, `disclosure_present`, and `ai_confidence`.

It shouldn't have existed. AI by Zapier already parses its own structured output and exposes each field as an individual data pill — `Brand Mentioned`, `Confidence Level`, `Disclosure Present`. They map straight into the Create Record step. I'd written a parser for a response that had already been parsed for me, because I assumed the integration behaved like a raw API call rather than reading what it actually returned.

The code is still in the repo at `zapier/02_parse_ai_response.py`, marked as unused. It's kept because the bug it hid is worth documenting.

### The bug it hid

The parser fed on `Object.to_json(Raw Output)`, which is the *entire* API response wrapper rather than the model's answer. `json.loads` succeeded on it. `parse_ok` was set to `True`. But the keys the parser wanted were nested one level deeper, so every lookup returned `None`, every field fell back to `pending`, and **nothing threw an exception**. The pipeline reported success while writing empty verdicts.

That's the failure mode worth remembering: not a crash, but a step that succeeds at the wrong thing. `parse_ok` was measuring whether the JSON was valid, not whether the fields were found. A truthful health check would have validated the keys, not the syntax.

### The keys were renamed underneath me

Once the nesting was fixed, the parser still failed. The prompt asked for `brand_mentioned` and `confidence`; the platform returned `"Brand Mentioned"` and `"Confidence Level"` — title-cased and spaced, regardless of what the prompt specified.

The general defence is to normalise keys at the boundary rather than trusting a contract you don't control:

```python
def norm_keys(d):
    return {
        str(k).strip().lower().replace(" ", "_").replace("-", "_"): v
        for k, v in d.items()
    }
```

In this pipeline that turned out to be unnecessary, because mapping the pills directly sidesteps the key names entirely. It's recorded here because the underlying problem — an upstream integration silently changing its output shape — is a real one, and the parser file demonstrates the fix.

### What was lost by deleting the step

The `pending` fallback. With the pills mapped directly, a failed classification writes an empty cell rather than an explicit `pending`. Empty is still distinguishable from `no`, so the compliance logic survives, but the intent is now implicit rather than stated.

A production version would keep a thin normaliser — not to parse anything, just to map unexpected values (`"unclear"`, `"N/A"`, empty) to an explicit `pending` before they reach the table.

---

## Reliability decisions

The parts of this that aren't the happy path:

**Retries distinguish transient from permanent failures.** A `403` means quota exhausted or a restricted key — retrying can't help, so it fails immediately with the API's own message rather than burning attempts. `429` and `5xx` get exponential backoff.

**Errors are isolated per channel.** One bad channel logs and continues instead of killing the run for every other creator.

**Optional API fields have defaults.** `likeCount` and `commentCount` disappear from the response entirely when a creator disables them. Every field read uses `.get()` with a fallback — this is the most common way the pipeline breaks against real channels rather than test data.

**Divide-by-zero guard on cost-per-engagement.** A video fetched minutes after upload genuinely has zero engagements. Returns `None` rather than throwing.

**Classification failures must never record `no`.** If the classifier fails and the row says "no disclosure found," a compliance report shows a clean result for a check that never ran. The distinction between "we looked and found nothing" and "we didn't find out" has to survive into the table.

**Missing descriptions fall back to the title, and say so.** Around a third of sampled videos had empty descriptions. Rather than generating a synthetic description — which would put fabricated text next to real API data with no way to tell them apart — the classifier analyses the title and stores `text_source: title_fallback`. A `no` verdict from a title alone is weaker evidence than one from a full description, and that column is what preserves the difference.

**Two timestamps, deliberately.** `published_at` is when the creator posted; `logged_at` is when the pipeline saw it. The gap between them is a monitoring signal — a widening drift means runs are being missed or the API is lagging.

---

## What the data showed

Tested against four public channels with deliberately different profiles:

| Channel | Subscribers | Engagements (5 videos) | Rate | Cost/engagement |
|---|---|---|---|---|
| Yogesh Rawat | 685K | 9,717,712 | $600 | $0.0001 |
| Untriggered with AminJaz | 500K | 1,032,090 | $450 | $0.0004 |
| Taarak Mehta Ka Ooltah Chashmah | 37.4M | 293,093 | $2,500 | $0.0085 |
| Netflix | 33.6M | 233,114 | $3,000 | $0.0129 |

A creator with 2% of Netflix's subscriber count generated roughly **40x more engagement at ~1/130th the cost per engagement**.

The two large accounts are brand channels pushing catalogue content, not creators with an invested audience — which is exactly the point. Subscriber count is a poor proxy for reach, and a system that surfaces this automatically is doing more than replacing data entry.

---

## Repo structure

```
├── local/
│   └── youtube_creator_fetch.py     standalone — validates the API layer
├── zapier/
│   ├── 01_fetch_youtube.py          Code step 2
│   ├── 02_parse_ai_response.py      UNUSED — see "What changed"
│   ├── 03_cost_per_engagement.py    Code step 6
│   └── prompt.md                    the classifier prompt
└── screenshots/
```

Files under `zapier/` are not standalone scripts. They run inside Zapier's Python runtime, read a global `input_data` dict, and assign to a global `output`.

`02_parse_ai_response.py` is not in the live pipeline. It's kept for the debugging history documented above.

`prompt.md` is versioned alongside the code deliberately. Prompts are configuration, and the JSON shape requested in the prompt is a contract the rest of the pipeline depends on.

---

## Running it locally

```bash
pip install requests pandas
python local/youtube_creator_fetch.py
```

Prompts for a YouTube Data API v3 key ([Google Cloud Console](https://console.cloud.google.com) → enable YouTube Data API v3 → create an API key). No OAuth or billing required. Writes `creator_posts.csv`.

The local script exists as a test harness: when something misbehaves inside Zapier, running the same calls locally isolates whether the problem is the logic or the platform runtime.

---

## Limitations

Known gaps, roughly in the order I'd fix them.

**No ground truth for the classifier.** Brand mention and disclosure detection are compliance signals, and right now there's no way to say how accurate they are. The fix is a hand-labelled evaluation set — a hundred videos, half known-sponsored — measured for precision and recall. A missed disclosure is a legal problem and a false flag costs thirty seconds of review, so the threshold should be tuned asymmetrically to over-flag and route uncertain cases to a human.

**The channel roster is a static input.** Adding a creator currently means editing the Zap rather than adding a row to Creators. The fix is a Find Records lookup ahead of step 2 that reads active creators at runtime — which also stops the pipeline spending API quota on paused campaigns before discarding them at step 4.

**Unmatched channels drop silently.** A video from a channel missing from the roster fails the filter and disappears with no trace. This should be a Path routing to a Slack alert, not a silent drop. Failures should be loud.

**No failure alerting.** If the Zap breaks at 3am, nobody finds out until someone checks the table. A separate Zap on the Zap Error trigger would close this.

**Secrets are stored as plain step inputs.** Zapier has no real secrets vault on this path — the API key lives in the Code step's input field. Acceptable for a personal project; a production version needs a proper secrets manager.

**Rate is not snapshotted onto rows.** Recomputing historical rows would apply today's payout rate to last month's posts.

---

## Where it breaks at scale

Three hard ceilings, all in Zapier rather than the logic:

**30-second cap on Code steps.** At roughly two sequential API calls per channel, the fetch tops out around 10–15 channels per run.

**Task cost scales linearly with volume.** The fan-out means every video consumes a task at each of steps 3–7. Twenty videos is over a hundred tasks per run; two thousand creators is not viable on any Zapier plan.

**No queue or backpressure.** A failed run doesn't retry the individual records that failed.

The design I'd move to: fetch and classification in a Lambda behind a queue, results pushed to Zapier via webhook. Keeps orchestration somewhere non-engineers can see and modify it, moves the volume-sensitive work into code where it's cheap. The threshold is somewhere around a few hundred creators.

---

## Screenshots

![Full pipeline — all 8 steps](screenshots/creator-content-tracker-canvas.png)


