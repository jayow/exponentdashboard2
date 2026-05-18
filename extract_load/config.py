"""Central config — env vars, paths, constants. No business logic."""
from __future__ import annotations
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
WAREHOUSE_PATH = Path(os.getenv("WAREHOUSE_PATH", DATA_DIR / "warehouse.duckdb"))

HELIUS_KEYS = [k for k in (os.getenv("HELIUS_KEY_1"), os.getenv("HELIUS_KEY_2")) if k]
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

EXTRACT_BATCH_SIZE = int(os.getenv("EXTRACT_BATCH_SIZE", "100"))
EXTRACT_CONCURRENCY = int(os.getenv("EXTRACT_CONCURRENCY", "12"))
EXTRACT_RETRY_MAX = int(os.getenv("EXTRACT_RETRY_MAX", "5"))

EXPONENT_PROGRAM = "ExponentnaRg3CQbW6dqQNZKXp7gtZ9DGMp1cwC4HAS7"

DATA_DIR.mkdir(parents=True, exist_ok=True)
