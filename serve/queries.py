"""SQL queries the serve layer runs against marts to produce slim JSONs.

Each query returns a dict of arrays ready to JSON-serialize. The frontend
expects time-series as {dates: [...], series: {name: [...]}, ...}.

Stub — Phase 4 implements alongside the frontend rewire.
"""
from __future__ import annotations

VOLUME_DAILY = """
    select
        date::varchar as date,
        side,
        sum(volume_usd) as volume_usd
    from analytics.trading_volume_daily
    group by 1, 2
    order by 1, 2
"""

VOLUME_BY_MARKET = """
    select
        date::varchar as date,
        market_key,
        sum(volume_usd) as volume_usd
    from analytics.trading_volume_daily
    group by 1, 2
    order by 1, 2
"""

TVL_DAILY = """
    select date::varchar as date, sum(tvl_usd) as tvl_usd
    from analytics.tvl_daily
    group by 1 order by 1
"""
