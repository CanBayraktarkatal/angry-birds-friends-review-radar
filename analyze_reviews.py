"""
Angry Birds Friends — player-review intelligence pipeline.

Adapted from the Review Radar pipeline. Same architecture, three changes:

  1. Two sources instead of one. Google Play and the Apple App Store are
     scraped, classified and briefed SEPARATELY, producing two independent
     datasets and two independent briefs.
  2. No competitors. The competitive-read section is replaced by a
     veteran-vs-new-player read, which is the more interesting question for a
     fourteen-year-old title.
  3. Taxonomy retuned from card games to a live-ops slingshot title:
     tournaments, leagues, events, economy, power-ups, level content.

State: data/<source>_classified.csv is the single source of truth per source.
A review is "new" if its review id is not already in that file — that's the
entire incremental mechanism, unchanged from the original.
"""

import json
import os
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import anthropic
import pandas as pd
import requests
from google_play_scraper import Sort, reviews

# ----------------------------- configuration -----------------------------

GAME = "Angry Birds Friends"

GOOGLE_PLAY_APP_ID = "com.rovio.angrybirdsfriends"
GOOGLE_PLAY_URL = (
    "https://play.google.com/store/apps/details?id=com.rovio.angrybirdsfriends&hl=en"
)

APP_STORE_APP_ID = "555936735"
APP_STORE_URL = "https://apps.apple.com/us/app/angry-birds-friends/id555936735"

# Countries to pull from. Google Play pages properly; Apple caps at 500 per
# country (10 pages x 50), most recent first.
COUNTRIES = ["us", "gb", "de", "br", "tr"]

REVIEWS_PER_COUNTRY = 300  # Google Play only; Apple is capped by the feed
APP_STORE_MAX_PAGES = 10

BATCH_SIZE = 25            # reviews per Claude API call
MODEL = "claude-sonnet-4-6"  # bump to "claude-sonnet-5" if your key has access
BRIEF_WINDOW_DAYS = 90     # each brief is written from the last N days

DATA_DIR = Path("data")
BRIEFS_DIR = Path("briefs")

SOURCES = {
    "google-play": {"label": "Google Play", "url": GOOGLE_PLAY_URL},
    "app-store": {"label": "App Store", "url": APP_STORE_URL},
}

client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from environment

# ------------------------------- scraping --------------------------------


def scrape_google_play() -> pd.DataFrame:
    """Pull the newest reviews per country, paginating as needed."""
    rows = []
    for country in COUNTRIES:
        collected, token = [], None
        while len(collected) < REVIEWS_PER_COUNTRY:
            try:
                batch, token = reviews(
                    GOOGLE_PLAY_APP_ID,
                    lang="en",
                    country=country,
                    sort=Sort.NEWEST,
                    count=min(200, REVIEWS_PER_COUNTRY - len(collected)),
                    continuation_token=token,
                )
            except Exception as e:
                print(f"  ! google play [{country}] failed: {e}")
                break
            collected.extend(batch)
            if token is None or not batch:
                break
            time.sleep(1)

        for r in collected:
            rows.append(
                {
                    "reviewId": r.get("reviewId"),
                    "country": country,
                    "userName": r.get("userName") or "Anonymous",
                    "score": r.get("score"),
                    "at": r.get("at"),
                    "title": "",
                    "content": r.get("content") or "",
                    "thumbsUpCount": r.get("thumbsUpCount", 0) or 0,
                    "appVersion": r.get("reviewCreatedVersion") or "unknown",
                }
            )
        print(f"  google play [{country}]: {len(collected)} fetched")

    df = pd.DataFrame(rows)
    if not df.empty:
        df["at"] = pd.to_datetime(df["at"], errors="coerce").dt.tz_localize(None)
    return df


APPLE_RSS = (
    "https://itunes.apple.com/{country}/rss/customerreviews/"
    "page={page}/id={app_id}/sortby=mostrecent/json"
)


def scrape_app_store() -> pd.DataFrame:
    """Apple's public review RSS feed. No API key, capped at 500 per country."""
    rows = []
    for country in COUNTRIES:
        found = 0
        for page in range(1, APP_STORE_MAX_PAGES + 1):
            url = APPLE_RSS.format(
                country=country, page=page, app_id=APP_STORE_APP_ID
            )
            try:
                resp = requests.get(
                    url, timeout=30, headers={"User-Agent": "abf-review-radar/1.0"}
                )
                if resp.status_code != 200:
                    break
                entries = resp.json().get("feed", {}).get("entry", [])
            except Exception as e:
                print(f"  ! app store [{country}] p{page} failed: {e}")
                break

            if not entries:
                break
            if isinstance(entries, dict):  # single-review feeds return a dict
                entries = [entries]

            for e in entries:
                if "im:rating" not in e:  # app metadata sometimes rides along
                    continue
                rows.append(
                    {
                        "reviewId": e.get("id", {}).get("label"),
                        "country": country,
                        "userName": e.get("author", {})
                        .get("name", {})
                        .get("label")
                        or "Anonymous",
                        "score": int(e["im:rating"]["label"]),
                        "at": e.get("updated", {}).get("label"),
                        "title": (e.get("title", {}).get("label") or "").strip(),
                        "content": (e.get("content", {}).get("label") or "").strip(),
                        # Apple exposes no upvote count; keep the column so the
                        # two datasets stay shape-compatible.
                        "thumbsUpCount": 0,
                        "appVersion": e.get("im:version", {}).get("label", "unknown"),
                    }
                )
                found += 1
            time.sleep(0.5)
        print(f"  app store [{country}]: {found} fetched")

    df = pd.DataFrame(rows)
    if not df.empty:
        df["at"] = pd.to_datetime(
            df["at"], errors="coerce", utc=True
        ).dt.tz_localize(None)
    return df


# ----------------------------- classification ----------------------------

CLASSIFY_SYSTEM = """You are a product analyst for a live free-to-play mobile game studio.
The game is Angry Birds Friends: a slingshot puzzle title, live since 2012, built around
weekly themed tournaments, league tiers and leaderboards, a 1v1 Star Cup mode, Bird Coins,
power-ups and a Feathers bird-levelling track.

You classify player reviews into structured data. Respond ONLY with a valid JSON array —
no markdown fences, no commentary.

For each review, output an object:
{
  "id": "<review id, copied exactly>",
  "category": "live_events | tournaments_fairness | economy_rewards | monetization | ads | progression_difficulty | bugs_performance | ux_ui | social_account | feature_request | praise | other",
  "sentiment": "positive | neutral | negative",
  "churn_risk": true/false,
  "tenure": "new | returning | veteran | unknown",
  "summary": "<one short sentence>"
}

Category definitions:
- live_events: tournament cadence, themed events, new level content, repetitiveness, "nothing new"
- tournaments_fairness: matchmaking, cheating or hacking accusations, rigged scoring, league tiers, leaderboard integrity, bots
- economy_rewards: Bird Coins, reward size for placement, power-up and booster supply, Feathers, currency scarcity or generosity
- monetization: prices, paywalls, purchase value, pay-to-win complaints, subscriptions
- ads: ad frequency, ad length, forced ads, misleading or broken ad creatives
- progression_difficulty: level difficulty, difficulty spikes, grind, Star Cup progression, Piggy Tower gating
- bugs_performance: crashes, freezes, loading, lost progress, lost purchases
- ux_ui: menus, readability, controls, aiming, layout changes
- social_account: Facebook connection, friends lists, login, account sync, cross-device
- feature_request: explicit asks for new modes, features or options
- praise: generally positive with no dominant specific theme
- other: none of the above

Pick the SINGLE most dominant category per review.

Tenure — infer only from what the review actually says, never guess:
- veteran: mentions playing for years, "since Facebook", "since 2012", long-time player
- returning: came back after a break
- new: just downloaded, just started
- unknown: no signal either way. Most reviews are unknown. Use it freely.

churn_risk: true only if the review states or strongly implies the player is quitting,
uninstalling, or has already stopped playing."""


def classify_batch(batch_df: pd.DataFrame) -> list:
    payload = [
        {
            "id": r.reviewId,
            "stars": int(r.score),
            "text": (f"{r.title}: " if r.title else "") + str(r.content)[:600],
        }
        for r in batch_df.itertuples()
    ]
    msg = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=CLASSIFY_SYSTEM,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
    )
    text = msg.content[0].text.strip()
    text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(text)


def classify_all(df: pd.DataFrame) -> pd.DataFrame:
    results = []
    n_batches = (len(df) + BATCH_SIZE - 1) // BATCH_SIZE
    for i in range(0, len(df), BATCH_SIZE):
        batch = df.iloc[i : i + BATCH_SIZE]
        try:
            results.extend(classify_batch(batch))
        except Exception as e:
            print(f"  batch {i // BATCH_SIZE + 1} failed ({e}) — retrying once...")
            time.sleep(5)
            try:
                results.extend(classify_batch(batch))
            except Exception as e2:
                print(f"  batch skipped: {e2}")
        print(f"  classified batch {i // BATCH_SIZE + 1}/{n_batches}")

    if not results:
        # Nothing came back. Return an empty frame with the right columns so
        # the caller doesn't blow up on a missing 'category'.
        print("  ! no classifications returned this run")
        return df.iloc[0:0].assign(
            category=None, sentiment=None, churn_risk=None, tenure=None, summary=None
        )

    labels = pd.DataFrame(results).rename(columns={"id": "reviewId"})
    return df.merge(labels, on="reviewId", how="inner")


# ------------------------------- the brief -------------------------------


def generate_brief(dfc: pd.DataFrame, n_new: int, source_key: str) -> str:
    label = SOURCES[source_key]["label"]

    cutoff = pd.Timestamp.now() - pd.Timedelta(days=BRIEF_WINDOW_DAYS)
    recent = dfc[dfc["at"] >= cutoff]
    if recent.empty:
        recent = dfc

    negative = recent[recent["score"] <= 2]

    stats = {
        "store": label,
        "window_days": BRIEF_WINDOW_DAYS,
        "new_reviews_this_run": n_new,
        "total_reviews_in_window": len(recent),
        "avg_rating": round(float(recent["score"].mean()), 2),
        "rating_distribution": recent["score"].value_counts().sort_index().to_dict(),
        "negative_share_pct": round(100 * len(negative) / len(recent), 1)
        if len(recent)
        else 0,
        "theme_counts": recent["category"].value_counts().to_dict(),
        "themes_in_1_2_star": negative["category"].value_counts().to_dict(),
        "avg_rating_by_theme": recent.groupby("category")["score"]
        .mean()
        .round(2)
        .to_dict(),
        "churn_risk_pct": round(100 * float(recent["churn_risk"].mean()), 1),
        "tenure_counts": recent["tenure"].value_counts().to_dict(),
        "avg_rating_by_tenure": recent.groupby("tenure")["score"]
        .mean()
        .round(2)
        .to_dict(),
        "themes_by_tenure": {
            t: g["category"].value_counts().head(5).to_dict()
            for t, g in recent.groupby("tenure")
            if t != "unknown"
        },
        "reviews_by_version": recent["appVersion"].value_counts().head(6).to_dict(),
        "avg_rating_by_version": recent.groupby("appVersion")["score"]
        .mean()
        .round(2)
        .to_dict(),
        "reviews_by_country": recent["country"].value_counts().to_dict(),
    }

    # Google Play exposes upvotes, so "most-upvoted" is a real signal there.
    # Apple doesn't, so fall back to the longest recent critical reviews —
    # effort is the closest available proxy for strength of feeling.
    if recent["thumbsUpCount"].sum() > 0:
        top = recent.sort_values("thumbsUpCount", ascending=False).head(15)
        top_label = "MOST-UPVOTED REVIEW SUMMARIES"
    else:
        top = (
            recent[recent["score"] <= 3]
            .assign(_len=lambda d: d["content"].str.len())
            .sort_values(["_len", "at"], ascending=False)
            .head(15)
        )
        top_label = (
            "LONGEST RECENT CRITICAL REVIEWS "
            "(this store exposes no upvote count, so length is the proxy)"
        )

    top_records = top[
        ["score", "thumbsUpCount", "category", "tenure", "appVersion", "summary"]
    ].to_dict(orient="records")

    prompt = f"""You are a senior product manager responsible for live operations on {GAME},
a fourteen-year-old free-to-play slingshot puzzle title built around weekly themed
tournaments, league tiers, a 1v1 Star Cup mode, Bird Coins, power-ups and a Feathers
bird-levelling track.

Below are aggregated review analytics from {label} over the last {BRIEF_WINDOW_DAYS} days.

Write a LiveOps brief with exactly these sections:

1. HEADLINE — the single most important takeaway, in one sentence.

2. TOP 3 PLAYER PAIN POINTS — each with the evidence and a hypothesis for a fix.
   Point each one at a specific live-ops lever: the event calendar, reward curve,
   matchmaking, difficulty pacing, offer pressure, or build health.

3. VETERAN VS NEW PLAYER READ — where long-tenured and new players diverge in what
   they complain about and how they rate the game. If the tenure data is too thin to
   support a read, say so plainly rather than inventing one.

4. RETENTION RISK — what the churn signals suggest, and which segment is most at risk.

5. RECOMMENDED NEXT TESTS — 2-3 concrete A/B test ideas, each as:
   hypothesis -> change -> success metric.

Be direct and specific. No filler. Never invent numbers that are not in the data below.
Where the sample is thin or skewed, say so rather than overstating confidence.

AGGREGATE STATS:
{json.dumps(stats, indent=2, default=str)}

{top_label}:
{json.dumps(top_records, indent=2, default=str)}"""

    msg = client.messages.create(
        model=MODEL,
        max_tokens=2500,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


def post_to_discord(brief: str) -> None:
    """Optional: post the brief to a Discord webhook if the secret is configured."""
    url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        return
    chunks = [brief[i : i + 1900] for i in range(0, len(brief), 1900)]
    for chunk in chunks:
        req = urllib.request.Request(
            url,
            data=json.dumps({"content": chunk}).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req)
        time.sleep(1)
    print("  posted brief to Discord.")


# --------------------------------- main ----------------------------------


def run_source(source_key: str) -> None:
    label = SOURCES[source_key]["label"]
    data_file = DATA_DIR / f"{source_key.replace('-', '_')}_classified.csv"
    briefs_dir = BRIEFS_DIR / source_key
    data_file.parent.mkdir(parents=True, exist_ok=True)
    briefs_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'=' * 60}\n{label}\n{'=' * 60}")

    # 1. Load master dataset; the set of already-seen review ids IS the state.
    if data_file.exists():
        master = pd.read_csv(data_file, parse_dates=["at"])
        seen_ids = set(master["reviewId"])
        print(f"Loaded master dataset: {len(master)} reviews already classified.")
    else:
        master = pd.DataFrame()
        seen_ids = set()
        print("No master dataset yet — first run will classify everything it scrapes.")

    # 2. Scrape and keep only unseen reviews.
    print("Scraping...")
    scraped = (
        scrape_google_play() if source_key == "google-play" else scrape_app_store()
    )
    if scraped.empty:
        print(f"! nothing scraped from {label}; skipping.")
        return

    scraped = scraped.dropna(subset=["content", "reviewId"])
    scraped = scraped[scraped["content"].str.strip() != ""]
    new = scraped[~scraped["reviewId"].isin(seen_ids)].reset_index(drop=True)
    print(f"New reviews since last run: {len(new)}")

    # 3. Classify only the new ones and append to the master file.
    if len(new) > 0:
        classified_new = classify_all(new)
        if not classified_new.empty:
            master = pd.concat([master, classified_new], ignore_index=True)
            master = master.drop_duplicates(subset="reviewId", keep="first")
            master.to_csv(data_file, index=False)
            print(f"Master dataset now holds {len(master)} classified reviews.")
    else:
        print("Nothing new to classify this run.")

    # 4. Generate this run's brief from the recent window of the full dataset.
    if master.empty:
        print("No data available — skipping brief.")
        return

    master["at"] = pd.to_datetime(master["at"], errors="coerce")
    master["churn_risk"] = master["churn_risk"].astype(bool)
    master["tenure"] = master["tenure"].fillna("unknown")

    brief = generate_brief(master, n_new=len(new), source_key=source_key)
    stamp = date.today().isoformat()
    header = (
        f"# LiveOps Brief — {GAME} — {label} — {stamp}\n\n"
        f"_Source: [{label}]({SOURCES[source_key]['url']}) · "
        f"{len(new)} new reviews this run · "
        f"{len(master)} classified in total_\n\n"
    )
    (briefs_dir / f"brief_{stamp}.md").write_text(header + brief + "\n")
    (briefs_dir / "latest.md").write_text(header + brief + "\n")
    print(f"Brief written to {briefs_dir}/brief_{stamp}.md")

    post_to_discord(f"**LiveOps Brief — {label} — {stamp}**\n\n{brief}")


def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "both"
    targets = list(SOURCES) if which == "both" else [which]
    for key in targets:
        if key not in SOURCES:
            print(f"Unknown source '{key}'. Use: google-play | app-store | both")
            continue
        run_source(key)
    print("\nDone.")


if __name__ == "__main__":
    main()
