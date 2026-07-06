"""Sequential Borsh walker for ExponentStrategyVault accounts.

Layout order per docs/STRATEGY_VAULTS_PROBE.md section 1, cross-checked
against @exponent-labs/exponent-vaults-idl@0.9.20. Everything after
offset 104 (start of token_entries vec) requires sequential parsing —
no fixed offsets past that point.
"""
from __future__ import annotations
import asyncio
import base64
import struct
from typing import Any

import base58


STRATEGY_VAULT_DISCRIMINATOR = bytes.fromhex("62e427c974d2270b")

# Anchor emit_cpi sentinel: inner self-CPIs that emit an Anchor event carry
# an 8-byte instruction-data prefix equal to sha256("anchor:event")[:8].
# These are NOT real instructions — they are event emissions we tag & skip.
ANCHOR_EMIT_CPI_MARKER = bytes.fromhex("e445a52e51cb9a1d")

# INSTRUCTIONS use 1-byte shank-style opcodes (verified against the on-chain
# IDL — every `discriminator` field is a single-element array like `[36]`).
# Do NOT confuse this with account/event discriminators, which ARE the usual
# 8-byte Anchor sha256 prefixes.
IX_DISCRIMINATORS: dict[bytes, str] = {
    bytes([0x00]): "initialize_vault",
    bytes([0x01]): "deposit_liquidity",
    bytes([0x02]): "add_policy",
    bytes([0x03]): "remove_policy",
    bytes([0x04]): "queue_withdrawal",
    bytes([0x05]): "fill_withdrawal",
    bytes([0x06]): "execute_withdrawal",
    bytes([0x07]): "update_price",
    bytes([0x08]): "manager_update_position",
    bytes([0x0a]): "sentinel_set_vault_flags",
    bytes([0x0c]): "stake_vote",
    bytes([0x0d]): "finalize_proposal",
    bytes([0x0e]): "unstake_vote",
    bytes([0x0f]): "cancel_proposal",
    bytes([0x10]): "execute_proposal",
    bytes([0x11]): "update_policy",
    bytes([0x12]): "wrapper_add_policy",
    bytes([0x13]): "wrapper_remove_policy",
    bytes([0x14]): "wrapper_update_policy",
    bytes([0x15]): "validate_interaction_hook",
    bytes([0x16]): "manage_vault_settings",
    bytes([0x18]): "initialize_prices",
    bytes([0x19]): "manage_prices",
    bytes([0x1a]): "update_twap_price",
    bytes([0x1c]): "cancel_withdrawal",
    bytes([0x1d]): "execute_withdrawal_from_reserves",
    bytes([0x1e]): "wrapper_execute_withdrawal",
    bytes([0x1f]): "collect_management_fee",
    bytes([0x20]): "make_sentinel_manager",
    bytes([0x21]): "update_policy_manager",
    bytes([0x22]): "init_proposal",
    bytes([0x23]): "append_proposal_actions",
    bytes([0x24]): "activate_proposal",
    bytes([0x25]): "sync_policy_authorities",
    bytes([0x26]): "migrate_vault_layout",
    bytes([0x27]): "refresh_aum",
    bytes([0x28]): "unblock_nav_aum_circuit_breaker",
    bytes([0x29]): "initialize_lp_metadata",
}

# Argument layouts per instruction (from idls/exponent_strategy_vault.json).
# Types supported by the primitive parser: u8, u16, u32, u64, i64, bool, pubkey.
# Anything else (structs, enums, vecs, options, strings) is opaque — parser
# stores raw hex under 'raw_hex' and logs a warning.
IX_ARGS: dict[str, list[tuple[str, str]]] = {
    "initialize_vault": [
        ("manager", "pubkey"),
        ("fee_treasury_lp_bps", "u16"),
        ("management_fee_bps", "u64"),
        ("reserves_share_bp", "u32"),
        ("fast_withdrawal_cut_bp", "u32"),
        ("token_entries", "vec<TokenEntry>"),
        ("max_lp_supply", "u64"),
        ("seed_id", "[u8; 8]"),
        ("lp_decimals", "u8"),
        ("roles", "VaultRoles"),
        ("proposal_vote_config", "option<ProposalVoteConfig>"),
    ],
    "deposit_liquidity": [("token_amount_in", "u64"), ("min_lp_out", "u64")],
    "add_policy": [("policy_config", "PolicyConfig")],
    "remove_policy": [],
    "queue_withdrawal": [("lp_amount", "u64")],
    "fill_withdrawal": [("fills", "vec<FillParam>")],
    "execute_withdrawal": [("token_transfer_accounts_until", "u8")],
    "update_price": [("updates", "vec<UpdatePriceInput>")],
    "manager_update_position": [("update", "PositionUpdate")],
    "sentinel_set_vault_flags": [("action", "VaultFlagAction")],
    "stake_vote": [("vote_choice", "VoteChoice"), ("lp_amount", "u64")],
    "finalize_proposal": [],
    "unstake_vote": [],
    "cancel_proposal": [],
    "execute_proposal": [],
    "update_policy": [("policy_config", "PolicyConfig")],
    "wrapper_add_policy": [("policy_config", "PolicyConfig")],
    "wrapper_remove_policy": [],
    "wrapper_update_policy": [("policy_config", "PolicyConfig")],
    "validate_interaction_hook": [("inner_instructions", "InnerInstructionsPayload")],
    "manage_vault_settings": [("actions", "vec<VaultSettingsAction>")],
    "initialize_prices": [],
    "manage_prices": [("actions", "vec<UpdatePriceAction>")],
    "update_twap_price": [("updates", "vec<UpdatePriceInput>")],
    "cancel_withdrawal": [],
    "execute_withdrawal_from_reserves": [("entry_idx", "u32"), ("lp_amount", "u64")],
    "wrapper_execute_withdrawal": [
        ("token_transfer_accounts_until", "u8"),
        ("entry_idx", "u32"),
    ],
    "collect_management_fee": [],
    "make_sentinel_manager": [],
    "update_policy_manager": [],
    "init_proposal": [("proposal_id", "u64")],
    "append_proposal_actions": [("actions", "vec<ProposalAction>")],
    "activate_proposal": [("params", "ProposalParams")],
    "sync_policy_authorities": [],
    "migrate_vault_layout": [],
    "refresh_aum": [],
    "unblock_nav_aum_circuit_breaker": [],
    "initialize_lp_metadata": [
        ("name", "string"),
        ("symbol", "string"),
        ("uri", "string"),
    ],
}

# From docs/STRATEGY_VAULTS_PROBE.md section 6a
EVENT_DISCRIMINATORS: dict[bytes, str] = {
    bytes.fromhex("52365a19645fc010"): "AumRefreshedEvent",
    bytes.fromhex("4444512db99bbac3"): "CancelProposalEvent",
    bytes.fromhex("76aa2fd99a72b7de"): "CancelWithdrawalEvent",
    bytes.fromhex("a95443aede8a107b"): "DepositLiquidityEvent",
    bytes.fromhex("990c2949ce72f8e9"): "ExecuteProposalEvent",
    bytes.fromhex("97c19bdb3653cfb1"): "ExecuteWithdrawalEvent",
    bytes.fromhex("ed361e1e6d30ada2"): "ExecuteWithdrawalFromReservesEvent",
    bytes.fromhex("2d1d7ab54fe0398d"): "FinalizeProposalEvent",
    bytes.fromhex("49f4e1855476264e"): "NavAumCircuitBreakerActivatedEvent",
    bytes.fromhex("0364d284e7289fa4"): "NavAumCircuitBreakerUnblockedEvent",
    bytes.fromhex("609c2218bef9f9a3"): "ProposeActionEvent",
    bytes.fromhex("01c27fff556f9b86"): "QueueWithdrawalEvent",
    bytes.fromhex("bdde87ce77a19960"): "StakeVoteEvent",
    bytes.fromhex("f1e09d6728290359"): "UnstakeVoteEvent",
    bytes.fromhex("81401b1858500df8"): "VaultFlagsUpdatedEvent",
}


# ---------- primitive readers ----------

def _read_u8(buf: bytes, off: int) -> tuple[int, int]:
    return buf[off], off + 1


def _read_u16_le(buf: bytes, off: int) -> tuple[int, int]:
    return struct.unpack_from("<H", buf, off)[0], off + 2


def _read_u32_le(buf: bytes, off: int) -> tuple[int, int]:
    return struct.unpack_from("<I", buf, off)[0], off + 4


def _read_u64_le(buf: bytes, off: int) -> tuple[int, int]:
    return struct.unpack_from("<Q", buf, off)[0], off + 8


def _read_i64_le(buf: bytes, off: int) -> tuple[int, int]:
    return struct.unpack_from("<q", buf, off)[0], off + 8


def _read_pubkey(buf: bytes, off: int) -> tuple[str, int]:
    return base58.b58encode(buf[off:off + 32]).decode(), off + 32


def _read_bytes(buf: bytes, off: int, n: int) -> tuple[bytes, int]:
    return buf[off:off + n], off + n


def _read_vec_len(buf: bytes, off: int) -> tuple[int, int]:
    return _read_u32_le(buf, off)


def _read_option(buf: bytes, off: int, payload_reader) -> tuple[Any, int]:
    tag = buf[off]
    off += 1
    if tag == 0:
        return None, off
    if tag != 1:
        raise ValueError(f"invalid Option tag {tag} at offset {off - 1}")
    return payload_reader(buf, off)


# ---------- enum-aware readers ----------

def _read_price_id(buf: bytes, off: int) -> tuple[dict, int]:
    tag = buf[off]
    off += 1
    if tag == 0:  # Simple { price_id: u64 }
        pid, off = _read_u64_le(buf, off)
        return {"variant": "Simple", "price_id": pid}, off
    if tag == 1:  # Multiply { price_ids: Vec<u64> }
        n, off = _read_vec_len(buf, off)
        ids = []
        for _ in range(n):
            v, off = _read_u64_le(buf, off)
            ids.append(v)
        return {"variant": "Multiply", "price_ids": ids}, off
    raise ValueError(f"unknown PriceId tag {tag} at offset {off - 1}")


def _read_withdrawal_period(buf: bytes, off: int) -> tuple[dict, int]:
    tag = buf[off]
    off += 1
    if tag == 0:  # Interval { lock_time_seconds: u32 }
        v, off = _read_u32_le(buf, off)
        return {"variant": "Interval", "lock_time_seconds": v}, off
    if tag == 1:  # WeeklySchedule { days_of_week u8, week_interval u8, reference_timestamp u32 }
        dow, off = _read_u8(buf, off)
        wi, off = _read_u8(buf, off)
        ref, off = _read_u32_le(buf, off)
        return {
            "variant": "WeeklySchedule",
            "days_of_week": dow,
            "week_interval": wi,
            "reference_timestamp": ref,
        }, off
    raise ValueError(f"unknown WithdrawalPeriodSettings tag {tag} at offset {off - 1}")


def _read_token_entry(buf: bytes, off: int) -> tuple[dict, int]:
    mint, off = _read_pubkey(buf, off)
    price_id, off = _read_price_id(buf, off)
    token_squads_account, off = _read_pubkey(buf, off)
    token_account_vault, off = _read_pubkey(buf, off)
    last_observed_amount, off = _read_u64_le(buf, off)
    n, off = _read_vec_len(buf, off)
    force_deallocate_policy_ids: list[int] = []
    for _ in range(n):
        v, off = _read_u64_le(buf, off)
        force_deallocate_policy_ids.append(v)
    return {
        "mint": mint,
        "price_id": price_id,
        "token_squads_account": token_squads_account,
        "token_account_vault": token_account_vault,
        "last_observed_amount": last_observed_amount,
        "force_deallocate_policy_ids": force_deallocate_policy_ids,
    }, off


def _read_orderbook_entry(buf: bytes, off: int) -> tuple[dict, int]:
    # orderbook pk, user_escrow_idx u32, mint pk, offer_idx_vec Vec<u32>,
    # price_id_pt PriceId, price_id_sy PriceId, base_mint pk
    orderbook, off = _read_pubkey(buf, off)
    user_escrow_idx, off = _read_u32_le(buf, off)
    mint, off = _read_pubkey(buf, off)
    n, off = _read_vec_len(buf, off)
    offer_idx_vec: list[int] = []
    for _ in range(n):
        v, off = _read_u32_le(buf, off)
        offer_idx_vec.append(v)
    price_id_pt, off = _read_price_id(buf, off)
    price_id_sy, off = _read_price_id(buf, off)
    base_mint, off = _read_pubkey(buf, off)
    return {
        "orderbook": orderbook,
        "user_escrow_idx": user_escrow_idx,
        "mint": mint,
        "offer_idx_vec": offer_idx_vec,
        "price_id_pt": price_id_pt,
        "price_id_sy": price_id_sy,
        "base_mint": base_mint,
    }, off


def _read_token_account_balance(buf: bytes, off: int) -> tuple[dict, int]:
    token_account, off = _read_pubkey(buf, off)
    price_id, off = _read_price_id(buf, off)
    last_observed_amount, off = _read_u64_le(buf, off)
    return {
        "token_account": token_account,
        "price_id": price_id,
        "last_observed_amount": last_observed_amount,
    }, off


def _read_token_account_entry(buf: bytes, off: int) -> tuple[dict, int]:
    token_mint, off = _read_pubkey(buf, off)
    n, off = _read_vec_len(buf, off)
    balances: list[dict] = []
    for _ in range(n):
        bal, off = _read_token_account_balance(buf, off)
        balances.append(bal)
    return {"token_mint": token_mint, "balances": balances}, off


def _read_reserve_price_mapping(buf: bytes, off: int) -> tuple[dict, int]:
    reserve, off = _read_pubkey(buf, off)
    price_id, off = _read_price_id(buf, off)
    return {"reserve": reserve, "reserve_price_id": price_id}, off


def _read_reserve_farm_mapping(buf: bytes, off: int) -> tuple[dict, int]:
    reserve, off = _read_pubkey(buf, off)
    farm_kind, off = _read_u8(buf, off)
    farm_state, off = _read_pubkey(buf, off)
    return {"reserve": reserve, "farm_kind": farm_kind, "farm_state": farm_state}, off


def _read_kamino_obligation_entry(buf: bytes, off: int) -> tuple[dict, int]:
    obligation, off = _read_pubkey(buf, off)
    lending_program_id, off = _read_pubkey(buf, off)
    quote_price_id, off = _read_price_id(buf, off)
    n_price, off = _read_vec_len(buf, off)
    reserve_price_mappings: list[dict] = []
    for _ in range(n_price):
        m, off = _read_reserve_price_mapping(buf, off)
        reserve_price_mappings.append(m)
    n_farm, off = _read_vec_len(buf, off)
    reserve_farm_mappings: list[dict] = []
    for _ in range(n_farm):
        m, off = _read_reserve_farm_mapping(buf, off)
        reserve_farm_mappings.append(m)
    min_price_status_flags, off = _read_u8(buf, off)
    return {
        "obligation": obligation,
        "lending_program_id": lending_program_id,
        "quote_price_id": quote_price_id,
        "reserve_price_mappings": reserve_price_mappings,
        "reserve_farm_mappings": reserve_farm_mappings,
        "min_price_status_flags": min_price_status_flags,
    }, off


def _read_obligation_type(buf: bytes, off: int) -> tuple[dict, int]:
    tag = buf[off]
    off += 1
    if tag == 0:  # KaminoObligation(KaminoObligationEntry)
        entry, off = _read_kamino_obligation_entry(buf, off)
        return {"variant": "KaminoObligation", **entry}, off
    raise ValueError(f"unknown ObligationType tag {tag} at offset {off - 1}")


def _read_yield_position_entry(buf: bytes, off: int) -> tuple[dict, int]:
    yield_position, off = _read_pubkey(buf, off)
    vault, off = _read_pubkey(buf, off)
    price_id_pt, off = _read_price_id(buf, off)
    price_id_sy, off = _read_price_id(buf, off)
    return {
        "yield_position": yield_position,
        "vault": vault,
        "price_id_pt": price_id_pt,
        "price_id_sy": price_id_sy,
    }, off


def _read_clmm_position_entry(buf: bytes, off: int) -> tuple[dict, int]:
    lp_position, off = _read_pubkey(buf, off)
    market, off = _read_pubkey(buf, off)
    price_id_pt, off = _read_price_id(buf, off)
    price_id_sy, off = _read_price_id(buf, off)
    return {
        "lp_position": lp_position,
        "market": market,
        "price_id_pt": price_id_pt,
        "price_id_sy": price_id_sy,
    }, off


def _read_loopscale_loan_entry(buf: bytes, off: int) -> tuple[dict, int]:
    loan, off = _read_pubkey(buf, off)
    return {"loan": loan}, off


def _read_loopscale_strategy_entry(buf: bytes, off: int) -> tuple[dict, int]:
    strategy, off = _read_pubkey(buf, off)
    return {"strategy": strategy}, off


def _read_kamino_farm_entry(buf: bytes, off: int) -> tuple[dict, int]:
    farm_state, off = _read_pubkey(buf, off)
    user_state, off = _read_pubkey(buf, off)
    return {"farm_state": farm_state, "user_state": user_state}, off


def _read_orca_whirlpool_position_entry(buf: bytes, off: int) -> tuple[dict, int]:
    position, off = _read_pubkey(buf, off)
    return {"position": position}, off


def _read_strategy_position(buf: bytes, off: int) -> tuple[dict, int]:
    tag = buf[off]
    off += 1
    if tag == 0:
        entry, off = _read_orderbook_entry(buf, off)
        return {"variant": "Orderbook", "orderbook": entry["orderbook"], "mint": entry["mint"], "base_mint": entry["base_mint"], "user_escrow_idx": entry["user_escrow_idx"], "offer_idx_vec": entry["offer_idx_vec"]}, off
    if tag == 1:
        entry, off = _read_token_account_entry(buf, off)
        return {"variant": "TokenAccount", "token_mint": entry["token_mint"], "balances": entry["balances"]}, off
    if tag == 2:
        entry, off = _read_obligation_type(buf, off)
        return {"variant": "Obligation", **entry}, off
    if tag == 3:
        entry, off = _read_yield_position_entry(buf, off)
        return {"variant": "YieldPosition", "yield_position": entry["yield_position"], "vault": entry["vault"]}, off
    if tag == 4:
        entry, off = _read_clmm_position_entry(buf, off)
        return {"variant": "ClmmPosition", "lp_position": entry["lp_position"], "market": entry["market"]}, off
    if tag == 5:
        entry, off = _read_loopscale_loan_entry(buf, off)
        return {"variant": "LoopscaleLoan", "loan": entry["loan"]}, off
    if tag == 6:
        entry, off = _read_loopscale_strategy_entry(buf, off)
        return {"variant": "LoopscaleStrategy", "strategy": entry["strategy"]}, off
    if tag == 7:
        entry, off = _read_kamino_farm_entry(buf, off)
        return {"variant": "KaminoFarm", "farm_state": entry["farm_state"], "user_state": entry["user_state"]}, off
    if tag == 8:
        entry, off = _read_orca_whirlpool_position_entry(buf, off)
        return {"variant": "OrcaWhirlpoolPosition", "position": entry["position"]}, off
    raise ValueError(f"unknown StrategyPosition tag {tag} at offset {off - 1}")


# ---------- top-level walker ----------

def decode_strategy_vault(raw: bytes) -> dict:
    if len(raw) < 8:
        raise ValueError("buffer too short for discriminator")

    out: dict[str, Any] = {}
    out["discriminator_ok"] = raw[:8] == STRATEGY_VAULT_DISCRIMINATOR
    off = 8
    last_field = "discriminator"

    try:
        # 0: nav_aum_circuit_breaker_state [u8; 32]
        nav_cb, off = _read_bytes(raw, off, 32)
        out["nav_cb_state_hex"] = nav_cb.hex()
        last_field = "nav_aum_circuit_breaker_state"

        # 1: squads_settings pubkey
        out["squads_settings"], off = _read_pubkey(raw, off)
        last_field = "squads_settings"

        # 2: squads_vault pubkey
        out["squads_vault"], off = _read_pubkey(raw, off)
        last_field = "squads_vault"

        # 3: token_entries Vec<TokenEntry>
        n, off = _read_vec_len(raw, off)
        token_entries: list[dict] = []
        for _ in range(n):
            te, off = _read_token_entry(raw, off)
            token_entries.append(te)
        out["token_entries"] = token_entries
        last_field = "token_entries"

        # 4: underlying_mint pubkey
        out["underlying_mint"], off = _read_pubkey(raw, off)
        last_field = "underlying_mint"

        # 5: mint_lp pubkey
        out["mint_lp"], off = _read_pubkey(raw, off)
        last_field = "mint_lp"

        # 6: token_lp_escrow pubkey
        out["token_lp_escrow"], off = _read_pubkey(raw, off)
        last_field = "token_lp_escrow"

        # 7: normal_withdrawal_cut_bp u16
        out["normal_withdrawal_cut_bp"], off = _read_u16_le(raw, off)
        last_field = "normal_withdrawal_cut_bp"

        # 8: fee_treasury pubkey
        out["fee_treasury"], off = _read_pubkey(raw, off)
        last_field = "fee_treasury"

        # 9: self_address pubkey
        out["self_address"], off = _read_pubkey(raw, off)
        last_field = "self_address"

        # 10: signer_bump [u8; 1]
        out["signer_bump"] = raw[off]
        off += 1
        last_field = "signer_bump"

        # 11: status_flags u8
        out["status_flags"], off = _read_u8(raw, off)
        last_field = "status_flags"

        # 12: financials VaultFinancials (96 bytes fixed)
        fin: dict[str, Any] = {}
        fin["lp_balance"], off = _read_u64_le(raw, off)
        fin["aum_in_base"], off = _read_u64_le(raw, off)
        fin["aum_in_base_in_positions"], off = _read_u64_le(raw, off)
        reserved_lp, off = _read_bytes(raw, off, 8)
        fin["reserved_lp_locked_for_withdrawal_hex"] = reserved_lp.hex()
        fin["pending_management_fee_lp"], off = _read_u64_le(raw, off)
        fin["last_management_fee_accrued_at"], off = _read_u64_le(raw, off)
        fin["pending_withdrawal_backlog_due_at"], off = _read_u32_le(raw, off)
        fin["instant_withdrawal_window_end"], off = _read_u32_le(raw, off)
        fin["instant_withdrawal_window_cap_aum"], off = _read_u64_le(raw, off)
        fin["instant_withdrawn_aum_in_window"], off = _read_u64_le(raw, off)
        reserved1, off = _read_bytes(raw, off, 8)
        fin["reserved1_hex"] = reserved1.hex()
        fin["total_lp_staked_in_votes"], off = _read_u64_le(raw, off)
        fin["total_lp_pending_withdrawals"], off = _read_u64_le(raw, off)
        out["financials"] = fin
        last_field = "financials"

        # 13: strategy_positions Vec<StrategyPosition>
        n, off = _read_vec_len(raw, off)
        strategy_positions: list[dict] = []
        for _ in range(n):
            sp, off = _read_strategy_position(raw, off)
            strategy_positions.append(sp)
        out["strategy_positions"] = strategy_positions
        last_field = "strategy_positions"

        # 14: max_aum_supply u64
        out["max_aum_supply"], off = _read_u64_le(raw, off)
        last_field = "max_aum_supply"

        # 15: seed_id [u8; 8]
        seed, off = _read_bytes(raw, off, 8)
        out["seed_id_hex"] = seed.hex()
        last_field = "seed_id"

        # 16: roles VaultRoles — 4 x Vec<pubkey>
        roles: dict[str, list[str]] = {}
        for role_name in ("manager", "curator", "allocator", "sentinel"):
            n, off = _read_vec_len(raw, off)
            members: list[str] = []
            for _ in range(n):
                pk, off = _read_pubkey(raw, off)
                members.append(pk)
            roles[role_name] = members
        out["roles"] = roles
        last_field = "roles"

        # 17: proposal_vote_config ProposalVoteConfig (65 bytes fixed)
        pvc: dict[str, Any] = {}
        pvc["min_voting_period_seconds"], off = _read_u32_le(raw, off)
        pvc["max_voting_period_seconds"], off = _read_u32_le(raw, off)
        pvc["default_voting_period_seconds"], off = _read_u32_le(raw, off)
        pvc["default_timelock_seconds"], off = _read_u32_le(raw, off)
        pvc["rejection_threshold_bps"], off = _read_u64_le(raw, off)
        pvc["next_proposal_id"], off = _read_u64_le(raw, off)
        pvc["voting_enabled"] = raw[off] != 0
        off += 1
        pvc["live_proposals_count"], off = _read_u8(raw, off)
        pvc["max_concurrent_live_proposals"], off = _read_u8(raw, off)
        reserved, off = _read_bytes(raw, off, 30)
        pvc["reserved_hex"] = reserved.hex()
        out["proposal_vote_config"] = pvc
        last_field = "proposal_vote_config"

        # 18: reserves_config VaultConfig
        rc: dict[str, Any] = {}
        rc["reserves_share_bp"], off = _read_u32_le(raw, off)
        rc["withdrawal_period"], off = _read_withdrawal_period(raw, off)
        rc["fast_withdrawal_cut_bp"], off = _read_u32_le(raw, off)
        rc["instant_withdrawal_window_limit_bp"], off = _read_u32_le(raw, off)
        rc["management_fee_bps"], off = _read_u64_le(raw, off)
        rc["withdrawal_cancel_cooldown_seconds"], off = _read_u32_le(raw, off)
        rc["max_swap_slippage_bp"], off = _read_u32_le(raw, off)
        rc["nav_aum_change_threshold_bps"], off = _read_u32_le(raw, off)
        # padding 5 + padding1 32 + padding2 32 + padding3 32 = 101 bytes
        _, off = _read_bytes(raw, off, 5)
        _, off = _read_bytes(raw, off, 32)
        _, off = _read_bytes(raw, off, 32)
        _, off = _read_bytes(raw, off, 32)
        out["reserves_config"] = rc
        last_field = "reserves_config"

    except (IndexError, struct.error) as e:
        raise ValueError(
            f"decode_strategy_vault: ran off buffer end while parsing "
            f"field '{last_field}' at offset {off} (buf len {len(raw)}): {e}"
        ) from e

    out["bytes_consumed"] = off
    return out


# ---------- CLI ----------

async def _main_async() -> None:
    from .config import RPC_ENDPOINTS, EXPONENT_STRATEGY_VAULT_PROGRAM
    from .solana_rpc_client import SolanaRpcClient

    if not RPC_ENDPOINTS:
        raise RuntimeError("No SOLANA_RPC_URLS configured in .env")

    disc_b58 = base58.b58encode(STRATEGY_VAULT_DISCRIMINATOR).decode()

    async with SolanaRpcClient(RPC_ENDPOINTS) as client:
        accts = await client.get_program_accounts(
            EXPONENT_STRATEGY_VAULT_PROGRAM,
            filters=[{"memcmp": {"offset": 0, "bytes": disc_b58}}],
        )

    print(f"fetched {len(accts)} ExponentStrategyVault accounts")
    print()

    ok = 0
    fail = 0
    fail_msgs: list[tuple[str, str]] = []

    for i, a in enumerate(accts):
        pubkey = a["pubkey"]
        data_field = a["account"]["data"]
        data_b64 = data_field[0] if isinstance(data_field, list) else data_field
        raw = base64.b64decode(data_b64)

        if i < 5:
            print("-" * 72)
            print(f"[{i}] {pubkey}")
            print(f"    data length      = {len(raw)}")
        try:
            decoded = decode_strategy_vault(raw)
            ok += 1
            if i < 5:
                print(f"    bytes_consumed   = {decoded['bytes_consumed']}  "
                      f"(delta = {len(raw) - decoded['bytes_consumed']})")
                fin = decoded["financials"]
                print(f"    lp_balance       = {fin['lp_balance']}")
                print(f"    aum_in_base      = {fin['aum_in_base']}")
                print(f"    aum_in_positions = {fin['aum_in_base_in_positions']}")
                print(f"    underlying_mint  = {decoded['underlying_mint']}")
                print(f"    mint_lp          = {decoded['mint_lp']}")
                print(f"    #token_entries   = {len(decoded['token_entries'])}")
                print(f"    #strategy_pos    = {len(decoded['strategy_positions'])}")
                print()
        except Exception as e:
            fail += 1
            fail_msgs.append((pubkey, str(e)))
            if i < 5:
                print(f"    DECODE FAILED    = {e}")
                print()

    print("=" * 72)
    print(f"SUMMARY: decoded ok = {ok}  failed = {fail}  total = {len(accts)}")
    if fail_msgs:
        print("failures:")
        for pk, msg in fail_msgs[:10]:
            print(f"  {pk}  ->  {msg}")


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
