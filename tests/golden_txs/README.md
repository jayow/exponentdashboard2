# Golden Tx Test Set

Hand-verified Solana txs used as accuracy regression for the indexer + dbt models.

## Adding a golden

1. Pick a tx (ideally one representative of each action: buyPt, sellPt, buyYt, sellYt, addLiq, removeLiq, claimYield, strip, redeemPt).
2. Open on [Solscan](https://solscan.io) and manually record:
   - Action (user intent)
   - Market
   - AMM pool's underlying flow (in/out)
   - PT delta at the AMM pool
   - Effective PT price
3. Drop a JSON file here:

```jsonc
{
  "signature": "...",
  "action": "buyYt",
  "market_key": "fragSOL-15DEC26",
  "expected": {
    "amm_underlying_flow": 1.9,
    "amm_pt_flow": 2.0,
    "pt_price": 0.95,
    "notional_usd": 285.0
  },
  "note": "Quiet day, single-trade tx. Verified manually 2026-05-18."
}
```

## Running

```bash
pytest tests/test_golden_txs.py
```

Test loads `raw_helius_tx` for the sig, runs it through the dbt models (or
queries `fct_swaps`), and asserts the numbers match the recorded expected values
within tolerance.
