-- Projection of raw_anchor_position_events (parsed by Python in
-- extract_load/extract_anchor_events.py — see that module for the parsing
-- logic and instruction → (leg, sign) mapping).
--
-- We parse in Python because the equivalent SQL (unnest logMessages +
-- window-fill the program ID) OOMs DuckDB on the full 695k-tx history
-- (35M+ log lines blow past 6 GiB temp).
{{ config(materialized='view') }}

select
    signature,
    block_time,
    signer,
    log_index,
    program_id,
    instruction_name,
    leg,
    action_sign,
    market_key
from {{ source('raw', 'raw_anchor_position_events') }}
