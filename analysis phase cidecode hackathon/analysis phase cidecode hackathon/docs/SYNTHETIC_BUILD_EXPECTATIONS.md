# Synthetic Build-Case Known-Answer Expectations

Recorded before detector code was written. Only cases `01` and `02` were read.
The detector implementation must satisfy both cases simultaneously with the same
general logic.

## Locked pattern 1 — duplicate cross-check

- `dup_case_01`: account `19378026502785`; independently confirm duplicate rows
  ending `000025, 000036, 000045, 000057, 000069, 000079` against their prior
  same-account payments. No unsupported extraction duplicate should remain.
- `dup_case_02`: account `9094834319063919`; independently confirm rows ending
  `000022, 000031, 000041, 000049, 000059, 000068, 000078`.

## Locked pattern 2 — failed/reversed transactions

- `dup_case_01`: detect reversal credits `19378026502785_000026` and
  `19378026502785_000058` with their preceding equal debit payments.
- `dup_case_02`: detect reversal credits `9094834319063919_000024`,
  `9094834319063919_000050`, and `9094834319063919_000079`.
- The detector must reach these results from amount, time, reference, and text
  evidence; the supplied `is_reversed` value is an answer for validation only.

## Locked pattern 3 — balance consistency

- Every non-duplicate row in all build files is expected to reconcile when the
  complete ledger sequence is used.
- The duplicate scenario is the edge case: excluded duplicate rows still changed
  the printed running balance, so they must remain continuity evidence. They must
  not cause the following eligible row to be mislabeled as an extraction error.
- Expected unexplained pattern-3 findings across build cases: zero.

## Locked pattern 4 — round trips

- `rt_case_01`: detect four time-respecting cycles over
  `21081385298452 → 51703956546 → 76469732163180 → 21081385298452`, using the
  mirrored pairs dated 12/14/24 February, 9/13/21 March, 3/8/17 April, and
  28 April/3 May/10 May. The 12 debit-side trigger IDs are:
  `21081385298452_000022`, `51703956546_000020`,
  `76469732163180_000025`, `21081385298452_000036`,
  `51703956546_000038`, `76469732163180_000039`,
  `21081385298452_000053`, `51703956546_000053`,
  `76469732163180_000055`, `21081385298452_000066`,
  `51703956546_000067`, `76469732163180_000072`.
- `rt_case_02`: detect five cycles over
  `16400202955 → 82389122072698 → 67947948262536 → 16400202955`. Debit-side
  trigger suffixes by cycle are `000021/000021/000022`,
  `000035/000036/000038`, `000053/000049/000051`,
  `000068/000068/000068`, and `000083/000081/000084` on the respective accounts.
- Isolated one-way UPI transfers are not round trips.

## Locked pattern 5 — transit / pass-through

- `tl_case_01`: account `721215922125` is the conduit. It receives seven large
  mirrored transfers from `6793198740`, forwards seven large transfers to
  `44883153319`, and has a high whole-account throughput ratio.
- `tl_case_02`: account `98598169111` is the conduit. It receives eight transfers
  from `28858586401516` and forwards eight to `64876803668202`.
- The small isolated outgoing transfer in each case is not sufficient evidence by
  itself; the account-level volume, balance of inflow/outflow, and connectivity
  must drive the result.

## Locked pattern 6 — accumulation

- `ac_case_01`: account `42963560676` receives 21 mirrored transfers from four
  observed source accounts, totals substantially more credit than debit, and is
  the expected accumulator.
- `ac_case_02`: account `82127478352` receives 24 mirrored transfers from four
  observed source accounts and is the expected accumulator.
- Ordinary accounts with one income source or substantial onward outflow should
  not be selected merely because they have a positive balance.

## Locked pattern 7 — structuring

- `st_case_01`: account `663458340862` has dense same-day outgoing groups on
  17 February (5 debits), 17 March (6), 14 April (7), and 12 May (5). Individual
  values cluster below the runtime upper amount cutoff while daily aggregates are
  large. At minimum the clearly mirrored groups on 17 March and 14 April must be
  detected.
- `st_case_02`: account `69200978299933` has dense groups on 22 February,
  22 March, 19 April, 17 May, and 14 June. At minimum the 22 March, 19 April, and
  14 June groups (5–6 mirrored debits each) must be detected.
- Thresholds must be derived anew for each case; the apparent nominal amount
  boundary must not appear in detector code.

## Locked pattern 8 — money-flow graph

- Every mirrored debit/credit pair must yield one canonical edge carrying both
  transaction IDs, never two money movements.
- `agg_case_01`: 78 canonical observed-account transfer edges; 75 terminate at
  `18808003162473`, and 3 continue to `35588709529484`.
- `agg_case_02`: 58 canonical observed-account transfer edges; 56 terminate at
  `86957120095922`, and 2 continue to `84392586324366`.
- Unresolved ordinary transactions remain in SQLite but create no guessed edge.

## Locked pattern 9 — circular flow

- `cf_case_01`: identify the three-account cycle
  `40154448554135 → 12853535777 → 192811835953 → 40154448554135`, supported by
  four repeated edge sets (12 canonical movements). The unrelated transfer from
  `40154448554135` to `7750283586793228` is not part of the cycle.
- `cf_case_02`: identify the four-account cycle
  `2264541082227 → 4412553087 → 14170744216 → 81137151773710 → 2264541082227`,
  supported by three repeated edge sets (12 movements). The unrelated transfer to
  `41535698295600` is not part of the cycle.

## Locked pattern 10 — money trail

- `mt_case_01`: explicitly trace credit `3545244589369467_000026` for 500,000.
  FIFO must visit subsequent debit IDs in ledger order, including the 150,000
  transfer `..._000027` and 200,000 transfer `..._000029`, with exact per-debit
  allocations until the trace is exhausted or the statement ends.
- `mt_case_02`: explicitly trace credit `47569602855_000029` for 500,000. FIFO
  begins with `..._000030`, then the 150,000 `..._000031`, and later the 200,000
  `..._000035`, preserving intervening debits in sequence.

## Cross-pattern build validations

- `ba_case_01/02` are additional short-window, high-velocity flow examples. They
  may legitimately trigger locked transit, structuring, graph, or trail logic but
  must not introduce an eleventh "burst" pattern.
- `combo_case_01/02` contain interacting graph and transaction behaviors and are
  used only after individual build expectations pass.

## Anti-overfitting attestations (pre-implementation)

For each locked pattern 1–10: this detector will not be tuned using the held-out
case for that pattern. Account IDs and transaction IDs above belong only in tests
and this expectation record; they must never appear in production detector code.
