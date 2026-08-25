# ABF Review Radar — Player Review Intelligence Pipeline

Automated pipeline that scrapes **Angry Birds Friends** reviews from Google Play and the
Apple App Store, classifies **only the new reviews** each run via the Claude API
(theme, sentiment, churn risk, player tenure), and generates a LiveOps brief per store.

Adapted from the Review Radar pipeline. Built by Can Bayraktarkatal.
Runs unattended on GitHub Actions every Monday.

**Sources**

- [Google Play](https://play.google.com/store/apps/details?id=com.rovio.angrybirdsfriends&hl=en) — `com.rovio.angrybirdsfriends`
- [App Store](https://apps.apple.com/us/app/angry-birds-friends/id555936735) — `555936735`

---

## What changed from Review Radar

| | Review Radar | ABF Review Radar |
|---|---|---|
| Sources | Google Play only | Google Play **and** App Store, kept fully separate |
| Datasets | One master CSV | One CSV **per store** |
| Output | One brief | One brief **per store** |
| Competitors | Three titles compared | None — dropped |
| Section 3 | Competitive read | **Veteran vs new player read** |
| Taxonomy | Card game themes | Live-ops slingshot themes |
| Extra field | — | `tenure` (new / returning / veteran / unknown) |

Everything else — the incremental `reviewId` mechanism, batched classification, the
brief structure, the Discord hook — is unchanged.

## How it works

1. **Scrape** — pulls the newest reviews per store, across five countries.
2. **Incremental filter** — `data/<store>_classified.csv` is the source of truth; any
   `reviewId` already in it is skipped, so each run only pays to classify what's new.
3. **Classify** — new reviews go to the Claude API in batches of 25 and come back as
   structured JSON (category, sentiment, churn risk, tenure, summary).
4. **Brief** — the last 90 days of classified data are summarized into a LiveOps brief
   in `briefs/<store>/`, and optionally posted to a Discord webhook.
5. **Commit** — the workflow commits the updated datasets and new briefs back to the
   repo, so history accumulates week over week.

## Brief structure

1. **HEADLINE** — single most important takeaway
2. **TOP 3 PLAYER PAIN POINTS** — evidence plus a hypothesis for a fix, each pointed at
   a specific live-ops lever
3. **VETERAN VS NEW PLAYER READ** — where long-tenured and new players diverge
4. **RETENTION RISK** — churn signals and the segment most at risk
5. **RECOMMENDED NEXT TESTS** — 2–3 A/B tests as `hypothesis -> change -> success metric`

## Output layout

```
data/
├── google_play_classified.csv    # master dataset, grows every run
└── app_store_classified.csv
briefs/
├── google-play/
│   ├── latest.md
│   └── brief_2026-08-25.md
└── app-store/
    ├── latest.md
    └── brief_2026-08-25.md
```

## Setup

This runs in two stages. **Colab first, then Actions.**

### Stage 1 — Colab (first run and taxonomy tuning)

Open `ABF_Review_Radar.ipynb` in [Google Colab](https://colab.research.google.com/).
It walks through:

1. A cheap smoke test on 50 reviews
2. A taxonomy quality check — watch the `other` share
3. Tuning `CLASSIFY_SYSTEM` and re-running until the categories fit
4. The full run across both stores
5. Committing the resulting CSVs back to this repo

That last step is the point: once the classified datasets are in the repo, every
future run is incremental and cheap.

### Stage 2 — GitHub Actions (weekly, unattended)

1. Add a repository secret named `ANTHROPIC_API_KEY`
   (Settings → Secrets and variables → Actions → New repository secret).
2. Optional: add `DISCORD_WEBHOOK_URL` to receive briefs in Discord.
3. Settings → Actions → General → Workflow permissions → **Read and write permissions**.
4. Trigger a manual run: Actions tab → "Weekly review analysis" → Run workflow.
   After that it runs every Monday automatically.

## Run locally

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=your-key-here

python analyze_reviews.py              # both stores
python analyze_reviews.py google-play  # one store
python analyze_reviews.py app-store
```

## Tuning

Constants at the top of `analyze_reviews.py`:

| Constant | Default | Notes |
|---|---|---|
| `COUNTRIES` | `us, gb, de, br, tr` | More countries = broader picture, slower and pricier |
| `REVIEWS_PER_COUNTRY` | `300` | Google Play only |
| `APP_STORE_MAX_PAGES` | `10` | Apple's hard cap: 500 reviews per country |
| `BATCH_SIZE` | `25` | Reviews per Claude call |
| `MODEL` | `claude-sonnet-4-6` | Bump to `claude-sonnet-5` if your key has access |
| `BRIEF_WINDOW_DAYS` | `90` | |

The `CLASSIFY_SYSTEM` taxonomy is the part most worth editing. After the first run, read
the CSV and check whether the categories are actually splitting reviews usefully — if
`other` is large, the taxonomy is wrong for this game.

## Known limits

- **Selection bias.** People who leave store reviews are disproportionately furious or
  delighted. This produces hypotheses, not conclusions.
- **Apple caps at 500 reviews per country**, most recent only, and exposes no upvote
  count — so the brief falls back to longest recent critical reviews as a proxy for
  strength of feeling. Google Play upvotes are a real signal; Apple's proxy is not.
  The two briefs are therefore not perfectly comparable.
- **`tenure` is inferred from review text**, so most reviews are `unknown`. Treat the
  veteran/new read as directional, and ignore it entirely when the sample is thin.
  The prompt instructs the model to say so, but check it yourself.
- **Google Play scraping is unofficial** and can break if Google changes endpoints.
  A failing country logs a warning and the run continues.
- **First run is the expensive one.** It classifies everything scraped. Subsequent runs
  only pay for genuinely new reviews — usually a small fraction.
