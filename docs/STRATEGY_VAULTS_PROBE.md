# Exponent Strategy Vaults — Verified Findings (Probe Report)

Authoritative reference for building the extraction pipeline. All figures below are cross-checked against the on-chain IDL (`@exponent-labs/exponent-vaults-idl@0.9.20`, program `sVau1tXvayVWfotzm9Ahcv2qfnnfRWttt78BCnNC6dD`) and the live probe of 58 vault accounts / 5 recent transactions.

## 0. Source Artifacts

- IDL file: `/Users/jakeolaso/Downloads/Claude Projects/ExponentDashboard2/idls/exponent_strategy_vault.json` (161,208 bytes, from npm `@exponent-labs/exponent-vaults-idl@0.9.20`).
- Probe script: `/Users/jakeolaso/Downloads/Claude Projects/ExponentDashboard2/extract_load/probe_strategy_vaults.py`.
- Probe stdout (1476 lines, 97 KB): `/Users/jakeolaso/.claude/projects/-Users-jakeolaso/5e856990-6c14-4333-819a-24be28f2633e/tool-results/bcorcfrcs.txt`.
- Package naming note: the npm package is `exponent-vaults-idl`; inside it, the JSON file is `exponent_vaults.json` and the account struct is `ExponentStrategyVault`. Prior plan references to `strategy-vaults-idl` were wrong.

## 1. `ExponentStrategyVault` Fields in Strict Borsh Order

The full struct is 19 fields. Sizes marked "variable" cannot be pre-computed; a decoder must walk them sequentially.

| Idx | Field | Type | Fixed size (bytes) |
|----:|---|---|---|
| 0 | `nav_aum_circuit_breaker_state` | `[u8; 32]` | 32 |
| 1 | `squads_settings` | `pubkey` | 32 |
| 2 | `squads_vault` | `pubkey` | 32 |
| 3 | `token_entries` | `Vec<TokenEntry>` | **variable** (u32 LE len + N * TokenEntry) |
| 4 | `underlying_mint` | `pubkey` | 32 |
| 5 | `mint_lp` | `pubkey` | 32 |
| 6 | `token_lp_escrow` | `pubkey` | 32 |
| 7 | `normal_withdrawal_cut_bp` | `u16` | 2 |
| 8 | `fee_treasury` | `pubkey` | 32 |
| 9 | `self_address` | `pubkey` | 32 |
| 10 | `signer_bump` | `[u8; 1]` | 1 |
| 11 | `status_flags` | `u8` | 1 |
| 12 | `financials` | `VaultFinancials` | 96 (see 1a) |
| 13 | `strategy_positions` | `Vec<StrategyPosition>` | **variable** (enum vec) |
| 14 | `max_aum_supply` | `u64` | 8 |
| 15 | `seed_id` | `[u8; 8]` | 8 |
| 16 | `roles` | `VaultRoles` | **variable** (4 x `Vec<pubkey>`) |
| 17 | `proposal_vote_config` | `ProposalVoteConfig` | 65 |
| 18 | `reserves_config` | `VaultConfig` | **variable** (contains `WithdrawalPeriodSettings` enum) |

### 1a. `VaultFinancials` (96 bytes, fixed)
`lp_balance u64` (8), `aum_in_base u64` (8), `aum_in_base_in_positions u64` (8), `reserved_lp_locked_for_withdrawal [u8;8]` (8), `pending_management_fee_lp u64` (8), `last_management_fee_accrued_at u64` (8), `pending_withdrawal_backlog_due_at u32` (4), `instant_withdrawal_window_end u32` (4), `instant_withdrawal_window_cap_aum u64` (8), `instant_withdrawn_aum_in_window u64` (8), `reserved1 [u8;8]` (8), `total_lp_staked_in_votes u64` (8), `total_lp_pending_withdrawals u64` (8). Total = 96.

### 1b. `ProposalVoteConfig` (65 bytes, fixed)
Four `u32`s (16) + two `u64`s (16) + `bool` (1) + two `u8`s (2) + `[u8;30]` reserved (30) = 65.

### 1c. `VaultConfig` (variable — contains `WithdrawalPeriodSettings` enum)
`reserves_share_bp u32` + `withdrawal_period WithdrawalPeriodSettings` (enum: 1 byte tag + variant payload) + `fast_withdrawal_cut_bp u32` + `instant_withdrawal_window_limit_bp u32` + `management_fee_bps u64` + `withdrawal_cancel_cooldown_seconds u32` + `max_swap_slippage_bp u32` + `nav_aum_change_threshold_bps u32` + `padding [u8;5]` + `padding1 [u8;32]` + `padding2 [u8;32]` + `padding3 [u8;32]`. All fields except `withdrawal_period` are fixed; **the enum makes total size variable unless both variants are the same length** (they're not, per IDL: `Interval` vs `WeeklySchedule`).

### 1d. `VaultRoles` (variable)
Four `Vec<pubkey>`: `manager`, `curator`, `allocator`, `sentinel`. Each is `u32 LE len + N * 32 bytes`.

### 1e. `TokenEntry` (variable)
`mint pubkey` (32) + `price_id PriceId` (enum, variable) + `token_squads_account pubkey` (32) + `token_account_vault pubkey` (32) + `last_observed_amount u64` (8) + `force_deallocate_policy_ids Vec<u64>` (variable, `u32 len + N * 8`).

### 1f. `StrategyPosition` (variable, enum with 9 variants)
`Orderbook | TokenAccount | Obligation | YieldPosition | ClmmPosition | LoopscaleLoan | LoopscaleStrategy | KaminoFarm | OrcaWhirlpoolPosition`. Anchor enum encoding = 1-byte tag + variant payload.

## 2. Fixed Byte Offsets (Live-Confirmed)

These are the only offsets safe to use as fixed constants. Everything after offset 104 requires sequential parsing.

| Offset range | Field | Confirmed live? |
|---|---|---|
| 0..8 | Anchor account discriminator `62 e4 27 c9 74 d2 27 0b` | Yes, matched on all 58 vaults |
| 8..40 | `nav_aum_circuit_breaker_state` (`[u8;32]`) | Yes; often all-zero on inactive vaults (per IDL doc: reuses former ALT slot) |
| 40..72 | `squads_settings` (pubkey) | Yes; decodes to non-zero base58 on every vault |
| 72..104 | `squads_vault` (pubkey) | Yes; decodes to non-zero base58 on every vault |
| 104 | Start of `token_entries` `u32 LE` length prefix | Yes; observed values 0, 1, 2 |
| 108..140 | First `TokenEntry.mint` when `len >= 1` | Yes; live mints include wSOL (`So11...`), USDC (`EPjF...`), JitoSOL (`J1to...`) |

**Do not** hardcode offsets past 104. Prior plans that assumed fixed offsets past this point were guessing — a Vec sits there.

## 3. Live Vault Population

- **58 `ExponentStrategyVault` accounts** discovered via `getProgramAccounts` filtered on the account discriminator.
- First 10 addresses (probed sample):

```
ZgMoh298sXFmJFtbPsqxw8tK3CBxtodaGx49gS5azjP  (used as event-probe target)
puiRBGikahXM4C7weFtNRFcs7bSUx8wVn1m5v6RA91o
utJ4Hftdqn3ZtkPy3Y59ea5E78PtJvBrrB6eGT9Mw7S
2BKGCfGFbVqrnezBwAEZkgDoTrwPeeYJ98ZFm4T5PM8T
2W6XKfA2cADpUNwdJwV88UoRMiWv13UgYSe8vXQ9s2cB
2cfFNGHrRsCcUoVEcfgsXpfueCH112Byebk7E1qqk2MJ
37zqKDTVf7noPjqJj6RHuWaK8YuxdQbMp1WRvstwemDp
3KPeSZeLeRixPudNC5JEkRuELyksobnksED41ZP5NewK
3dEDX4qcq3QG8zkyznojzc4W15XY36KbWTkQiMUKkZEE
3prf7fB2eqkrY4gcLK3n7C6iStNhGRUf4fWjVQfog7ig
```

(48 more in the persisted probe output.)

## 4. Event Channel — Verified

**Target vault:** `ZgMoh298sXFmJFtbPsqxw8tK3CBxtodaGx49gS5azjP`.
**Probe window:** 5 most recent successful transactions.
**Instruction mix observed:** `MigrateVaultLayout`, `QueueWithdrawal`, `UpdatePrice`, `DepositLiquidity`, `WrapperAddPolicy`, `FillWithdrawal`.

Findings:

- `Program data:` log lines: **0 across all 5 transactions**. The strategy-vault program does **not** emit Anchor events via the standard log channel.
- Self-CPIs to the vault program **do** occur, but their instruction-data prefix is **not** the Anchor `event_cpi` marker `e4 45 a5 2e 51 cb 9a 1d` and does **not** match any of the 15 event discriminators from the IDL. Observed prefixes (e.g. `06 57 06 15`, `05 01 00 00`, `0d 0e 96 09`) are ordinary global instruction discriminators — internal nested workflow steps on the same program.
- Squads program (`SMRTzfY6DfH5ik3TKiyLFfXexV8uSG3d2UksSCYdunG`) emits its own `LogEvent` from CPIs; those are Squads events, not strategy-vault events.
- Structured payloads that **do** appear on-chain in the 5-tx window:
  1. `Program return: sVau1t... <base64>` after `QueueWithdrawal` (176-byte payload) and `DepositLiquidity` (264-byte payload). These are `set_return_data` outputs containing post-execution state (pubkeys, amounts, timestamps visible in hex).
  2. `Program log: Instruction: <PascalName>` — Anchor's default per-ix marker, sufficient to identify the outer instruction name.

**Conclusion — event channel we implement first:** neither log-based nor self-CPI-based Anchor event decoding is viable for this program in current live traffic. Two implementable channels, in order:

1. **Instruction-level extraction (implement first).** Read the outer `transaction.message.instructions` array, filter to program `sVau1t...`, match the leading 8 bytes to the ix-discriminator table (Section 6), and decode the ix args from the IDL. Correlate with `Program return:` payloads (Section 5) for post-state.
2. **Account-state diffing.** Snapshot `ExponentStrategyVault.financials` (LP balance, AUM, pending withdrawals) at successive slots to reconstruct deltas.

Anchor event decoding stays in the codebase as a defensive path only, using the discriminators below, in case a future program version turns emits on for the rarer paths (`AumRefreshedEvent`, `ProposeActionEvent`). If we need to prove those paths never emit, widen the probe to `limit=1000` with pagination and grep for `Program data:`.

## 5. Transaction Probed & Payloads Observed

Vault: `ZgMoh298sXFmJFtbPsqxw8tK3CBxtodaGx49gS5azjP`. The probe iterated the 5 latest successful signatures — exact signature strings are captured in the persisted stdout (`bcorcfrcs.txt`). Anchor events detected: **0**. Non-Anchor structured payloads detected: 2 `Program return:` blobs (one on `QueueWithdrawal` = 176 bytes, one on `DepositLiquidity` = 264 bytes).

## 6. Discriminators

### 6a. Event Discriminators (`sha256("event:<PascalCaseName>")[:8]`)

All 15 events from the IDL:

| Event | Discriminator (hex) |
|---|---|
| `AumRefreshedEvent` | `52 36 5a 19 64 5f c0 10` |
| `CancelProposalEvent` | `44 44 51 2d b9 9b ba c3` |
| `CancelWithdrawalEvent` | `76 aa 2f d9 9a 72 b7 de` |
| `DepositLiquidityEvent` | `a9 54 43 ae de 8a 10 7b` |
| `ExecuteProposalEvent` | `99 0c 29 49 ce 72 f8 e9` |
| `ExecuteWithdrawalEvent` | `97 c1 9b db 36 53 cf b1` |
| `ExecuteWithdrawalFromReservesEvent` | `ed 36 1e 1e 6d 30 ad a2` |
| `FinalizeProposalEvent` | `2d 1d 7a b5 4f e0 39 8d` |
| `NavAumCircuitBreakerActivatedEvent` | `49 f4 e1 85 54 76 26 4e` |
| `NavAumCircuitBreakerUnblockedEvent` | `03 64 d2 84 e7 28 9f a4` |
| `ProposeActionEvent` | `60 9c 22 18 be f9 f9 a3` |
| `QueueWithdrawalEvent` | `01 c2 7f ff 55 6f 9b 86` |
| `StakeVoteEvent` | `bd de 87 ce 77 a1 99 60` |
| `UnstakeVoteEvent` | `f1 e0 9d 67 28 29 03 59` |
| `VaultFlagsUpdatedEvent` | `81 40 1b 18 58 50 0d f8` |

### 6b. Instruction Discriminators (`sha256("global:<snake_case>")[:8]`)

| Instruction | Discriminator (hex) |
|---|---|
| `deposit_liquidity` | `f5 63 3b 19 97 47 e9 f9` |
| `queue_withdrawal` | `99 08 b0 eb bd 8c 92 df` |
| `execute_withdrawal` | `71 79 cb e8 89 8b f8 f9` |
| `execute_withdrawal_from_reserves` | `77 57 cd ae 42 57 ad 9f` |
| `refresh_aum` | `dd d5 46 cb a4 6f 8e 04` |
| `collect_management_fee` | `6e 87 0d 56 06 ac 8b 55` |
| `manager_update_position` | `a8 94 44 a6 9c a6 d4 b4` |

## 7. Constants

- Program ID: `sVau1tXvayVWfotzm9Ahcv2qfnnfRWttt78BCnNC6dD`
- Squads program (seen in CPIs): `SMRTzfY6DfH5ik3TKiyLFfXexV8uSG3d2UksSCYdunG`
- Account discriminator (`ExponentStrategyVault`): `62 e4 27 c9 74 d2 27 0b`
- Anchor `event_cpi` prefix (reference, not observed): `e4 45 a5 2e 51 cb 9a 1d`

## 8. Correction: instruction discriminators are 1-byte shank, not 8-byte Anchor

Section 6b above is **wrong for instructions** and must be ignored. It was derived by assuming Anchor-style `sha256("global:<name>")[:8]` prefixes; the on-chain program is actually built with shank, whose instructions carry a single-byte opcode.

Direct inspection of `idls/exponent_strategy_vault.json` confirms:

- **Accounts and events**: 8-byte Anchor sha256 discriminators. Example verified live: `ExponentStrategyVault = [98, 228, 39, 201, 116, 210, 39, 11]` = `62 e4 27 c9 74 d2 27 0b`. Section 6a's event table is correct and stays.
- **Instructions**: 1-byte shank opcodes. Every `discriminator` field in the IDL's `instructions` array is a single-element list, e.g. `[36]` for `refresh_aum` (= `0x27`). Total 38 instructions.

The extractor must slice `data_bytes[:1]` (not `[:8]`) for the disc and parse args from `data_bytes[1:]`.

### 8a. Anchor `emit_cpi!` sentinel

Some inner instructions targeting the vault program start with the 8-byte prefix `e4 45 a5 2e 51 cb 9a 1d` (= `sha256("anchor:event")[:8]`). These are not real instructions — the program self-CPIs to itself just to log an event. Recognise the marker, tag as `anchor_event_cpi`, and skip arg parsing. The next 8 bytes after the sentinel are the event discriminator from Section 6a.

The observation in Section 4 that this prefix was "not observed" reflected the 5-transaction probe window — the sentinel does exist in the wire format and must be handled defensively regardless.

### 8b. Corrected instruction discriminator table

| Opcode (hex) | Instruction |
|---:|---|
| `00` | `initialize_vault` |
| `01` | `deposit_liquidity` |
| `02` | `add_policy` |
| `03` | `remove_policy` |
| `04` | `queue_withdrawal` |
| `05` | `fill_withdrawal` |
| `06` | `execute_withdrawal` |
| `07` | `update_price` |
| `08` | `manager_update_position` |
| `0a` | `sentinel_set_vault_flags` |
| `0c` | `stake_vote` |
| `0d` | `finalize_proposal` |
| `0e` | `unstake_vote` |
| `0f` | `cancel_proposal` |
| `10` | `execute_proposal` |
| `11` | `update_policy` |
| `12` | `wrapper_add_policy` |
| `13` | `wrapper_remove_policy` |
| `14` | `wrapper_update_policy` |
| `15` | `validate_interaction_hook` |
| `16` | `manage_vault_settings` |
| `18` | `initialize_prices` |
| `19` | `manage_prices` |
| `1a` | `update_twap_price` |
| `1c` | `cancel_withdrawal` |
| `1d` | `execute_withdrawal_from_reserves` |
| `1e` | `wrapper_execute_withdrawal` |
| `1f` | `collect_management_fee` |
| `20` | `make_sentinel_manager` |
| `21` | `update_policy_manager` |
| `22` | `init_proposal` |
| `23` | `append_proposal_actions` |
| `24` | `activate_proposal` |
| `25` | `sync_policy_authorities` |
| `26` | `migrate_vault_layout` |
| `27` | `refresh_aum` |
| `28` | `unblock_nav_aum_circuit_breaker` |
| `29` | `initialize_lp_metadata` |

Gaps (`09`, `0b`, `17`, `1b`) are unused by the current IDL. `0xe4` is **not** an instruction opcode — any inner ix beginning with `e4` is the Anchor `emit_cpi` sentinel described in 8a.
