"""Central config — env vars, paths, constants. No business logic."""
from __future__ import annotations
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
WAREHOUSE_PATH = Path(os.getenv("WAREHOUSE_PATH", DATA_DIR / "warehouse.duckdb"))

# Comma-separated full RPC endpoint URLs — Helius, QuickNode, Chainstack,
# Alchemy, public RPC. The client round-robins across them.
RPC_ENDPOINTS = [u.strip() for u in os.getenv("SOLANA_RPC_URLS", "").split(",") if u.strip()]

# RPS budget: batch size × concurrency × completion-rate per second.
# Helius Developer plan caps at ~50 RPS (each method in a JSON-RPC batch
# counts as a request). 2 × 15 ≈ 30 in-flight calls — safe headroom.
# Bump on higher-tier plans or with multiple endpoints in SOLANA_RPC_URLS.
EXTRACT_BATCH_SIZE = int(os.getenv("EXTRACT_BATCH_SIZE", "15"))
EXTRACT_CONCURRENCY = int(os.getenv("EXTRACT_CONCURRENCY", "2"))
EXTRACT_RETRY_MAX = int(os.getenv("EXTRACT_RETRY_MAX", "5"))

EXPONENT_CORE_PROGRAM = "ExponentnaRg3CQbW6dqQNZKXp7gtZ9DGMp1cwC4HAS7"
EXPONENT_CLMM_PROGRAM = "XPC1MM4dYACDfykNuXYZ5una2DsMDWL24CrYubCvarC"

# Every Exponent-related tx invokes one of these — scanning both is exhaustive.
EXPONENT_PROGRAMS = [EXPONENT_CORE_PROGRAM, EXPONENT_CLMM_PROGRAM]


DATA_DIR.mkdir(parents=True, exist_ok=True)
