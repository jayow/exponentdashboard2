# Deployment guide

## Architecture overview

```
                                  ┌─────────────────────┐
                                  │  GitHub Actions     │
                                  │  (.github/workflows │
                                  │   /refresh.yml)     │
                                  │                     │
              ┌───── cron ───────▶│  daily 02:30 UTC    │
              │                   │                     │
              │                   │  1. restore slim DB │
              │                   │     from cache      │
              │                   │  2. fetch new tx    │  ← Helius RPC
              │                   │     via Helius      │
              │                   │  3. dbt incremental │
              │                   │  4. build JSON      │
              │                   │  5. compact (NULL   │
              │                   │     out payloads)   │
              │                   │  6. save cache      │
              │                   │  7. commit JSON     │
              │                   │     to main         │
              │                   └─────────┬───────────┘
              │                             │
              │                             │ git push
              │                             ▼
              │                   ┌─────────────────────┐
              │                   │  GitHub repo        │
              │                   │  (web/public/*.json)│
              │                   └─────────┬───────────┘
              │                             │
              │                             │ auto-deploy on push
              │                             ▼
              │                   ┌─────────────────────┐
              │                   │  Cloudflare Pages   │ ← static host (free)
              │                   │  or Vercel          │
              │                   │  serves Next export │
              │                   └─────────────────────┘
              │
              │
              ▼
        ┌───────────────────────────────┐
        │  Your laptop                  │  Optional: lets you run
        │  data/warehouse.duckdb (39GB) │  --full-refresh, regenerate
        │  Full history with payloads   │  slim seed, etc.
        └───────────────────────────────┘
```

## One-time setup

### 1. Create the GitHub repo + push code

```bash
git remote add origin https://github.com/<you>/exponentdashboard2.git
git push -u origin main --tags
```

### 2. Configure GHA secrets

In repo Settings → Secrets and variables → Actions, add:

| Secret | Value |
|---|---|
| `SOLANA_RPC_URLS` | Your comma-separated RPC endpoints (Helius + fallbacks) |

`GITHUB_TOKEN` is auto-provided by GHA — no setup.

### 3. Upload the warehouse seed (one-time bootstrap)

The first GHA run needs an initial warehouse. Generate the slim DB locally
and upload it as a release:

```bash
# Locally — produce a fresh slim warehouse from your full one
python -m extract_load.build_slim_db
gzip -k data/warehouse_slim.duckdb       # data/warehouse_slim.duckdb.gz

# Create a release tagged 'warehouse-seed' with the gzipped slim attached
gh release create warehouse-seed \
    data/warehouse_slim.duckdb.gz \
    --title "Warehouse seed" \
    --notes "Initial slim warehouse for GHA cold-start. ~1 GB."
```

After the first GHA run succeeds, the seed release is no longer used —
the GHA cache takes over. Re-upload occasionally if you want a faster
cold-start when the cache evicts (GHA evicts unused caches after 7 days).

### 4. Configure the static host

#### Cloudflare Pages (recommended)

1. Cloudflare dashboard → Pages → "Connect to Git" → pick this repo
2. Build settings:
   - Framework preset: **Next.js (Static HTML Export)**
   - Build command: `cd web && npm install && npm run build`
   - Build output directory: `web/out`
3. Save. First deploy starts immediately. Subsequent pushes auto-deploy.

#### Vercel (alternative)

1. Vercel dashboard → "Add New… Project" → pick this repo
2. Root directory: `web`
3. Framework: Next.js (auto-detected)
4. Output: `out` (Next will produce because `output: 'export'`)
5. Save.

Both are free tier. Cloudflare has slightly higher build/request limits;
Vercel has better Next.js integration. Either works.

## Daily refresh cycle

Once set up, GHA runs every day at 02:30 UTC:

1. Restores `data/warehouse.duckdb` from cache (≈1 GB)
2. Runs the whole `extract → transform → serve` pipeline (~20 min)
3. Compacts the warehouse (strips payloads)
4. Saves cache back (≈1 GB)
5. Commits refreshed `web/public/*.json` to `main`
6. Cloudflare/Vercel auto-deploys the static site

Manual trigger anytime: repo → Actions → "Daily Dashboard Refresh" → "Run workflow".

## Local development

The full warehouse (39 GB) stays on your laptop — never pushed to git
(`.gitignore` covers it). For local refresh:

```bash
make all  # extract → transform → serve
```

To regenerate the slim seed and update the GitHub release:

```bash
python -m extract_load.build_slim_db
gzip -kf data/warehouse_slim.duckdb
gh release upload warehouse-seed data/warehouse_slim.duckdb.gz --clobber
```

## Tradeoffs / limitations

- **GHA cache 10 GB limit**: slim warehouse is ~1 GB so plenty of headroom.
  Watch the size if `stg_token_changes` or other persisted tables balloon.
- **dbt `--full-refresh` doesn't work in cloud** — needs full payload history.
  Always use incremental in GHA. To rebuild from scratch, do it locally
  and re-upload the seed.
- **Cache eviction**: GHA evicts caches unused for 7 days. If the cron
  misses for >7 days, next run falls back to the seed release (cold start
  ≈ adds 2-3 min for download). Keep the seed release fresh by re-running
  step 3 every few weeks.
- **Wallet shards**: `web/public/wallet/<addr>.json` files — there are
  ~37,000 of them. They DO get committed alongside the main JSON. Git
  history grows ~5 MB/day. Periodic `git filter-repo` cleanup if it
  bothers you.
- **No auth**: the dashboard serves on-chain data so it's public anyway,
  but worth noting if you wanted to add private metrics later.

## Cost estimate

| Item | Cost |
|---|---|
| GHA minutes (daily refresh ~20 min × 30 = 10 hrs/mo) | $0 (free tier 2000 min/mo for public repos) |
| GHA cache storage (~1 GB) | $0 (free up to 10 GB) |
| Cloudflare Pages | $0 (free tier: 500 builds/mo, unlimited bandwidth) |
| Helius RPC | Whatever your existing plan is |
| Domain (optional) | $10-15/yr if you want a custom domain |

**Total: $0** for a private repo + free tier hosting, assuming your
Helius plan covers ~5k requests/day for the daily incremental refresh.
