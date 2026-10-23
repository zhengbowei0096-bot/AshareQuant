# Model Upgrades (模型研究试用与验证)

## Daily use (每日使用)

Run `python -m asharequant.cli iteration --root .` after reliable closing data is available. The existing daily workflow calls it at the usual Beijing 18:00 schedule. Open `reports/iteration/latest/index.html` or the model research section of the daily overview. No extra chat polling is introduced.

The default path is `slot-v2`, separate from legacy frozen experiments. Each stock and 1/5/20-session horizon is a distinct slot. A completed search is checkpointed once per data session; no new confirmed first-daily forecast outcomes means no new search. Slots already under prospective validation keep their recipe; other slots may be researched daily. Original ridge fitting, features, scoring, fees and risk configurations are unchanged.

## Fast research trial (快速研究试用)

The finite family remains three ridge penalties times three existing feature subsets. Compare each candidate with that slot's accepted recipe, or the existing research recipe when none is accepted. Require lower historical mean absolute error (any positive amount, no fixed 2%), at least three of four chronological blocks improved, lower error over the last 20 matured historical forecasts and recent direction accuracy no worse than the comparator. These are historical screening checks, **not independent proof**.

A passing recipe can enter the next complete daily forecast as **research trial**. Both the daily forecast and the daily research source used by intraday simulation record its identity and feature columns. Each day's weights are refitted using the original algorithm and only then-matured labels. Existing same-day frozen predictions/plans are never replaced; a newly eligible trial appears at the next unsealed forecast generation. Intraday incomplete daily-bar inference remains unvalidated research regardless of a daily prediction's status.

## Independent validation (独立验证)

At selection, freeze the recipe, comparator, training cap/configuration, implementation identity, start session and ordinal test-window rule. This is a prospective **prequential daily-refit procedure** test, not a claim that one fixed coefficient vector was tested. Earlier matured observations may enter later training exactly as prescribed; no test result is used to tune or select parameters. Issues are sealed after their origin close and before the next opening deadline. No missing historical prediction may be backfilled.

First trials require at least 60/90/253 prediction origins for 1/5/20-session horizons and maturity of the final outcome. The 20-session horizon cannot provide many independent outcomes in a few weeks. These windows concern independent acceptance only and do not delay the research trial. Calendar dates beyond verified exchange-calendar coverage are shown as unknown rather than guessed.

Each slot is evaluated once at its frozen final date. Use non-overlapping pairs, positive paired error-gain confidence bounds, non-decreasing direction accuracy versus the comparator, the original direction-confidence checks, and a sign test. There is no fixed minimum percentage improvement. A lifetime research error budget of 5% is allocated over up to 150 stock/horizon slots and repeated attempts as `0.05 / (150 * k * (k+1))`. The minimum independent count/window grows if needed so the predeclared sign test is attainable. This is deliberately conservative and is not a promise of timely acceptance or profits. A universe change or code/config mismatch cannot silently reset an attempt counter.

Accepted slots receive a **prediction recipe** release only. Rejected trials stop being selected; the prior valid accepted recipe or baseline remains. A partial pass never upgrades other stocks/horizons. Historical trial predictions and simulated trades are retained. Probability calibration and the existing full portfolio cost, drawdown, turnover and stability gates are not granted or relaxed by this release. Legacy whole-portfolio lifecycle experiments retain their original 2%/672-origin rules and immutable evidence for compatibility; they are not the gate for starting a research trial.

Temporal validation and model-selection separation follow [scikit-learn's leakage guidance](https://scikit-learn.org/stable/common_pitfalls.html#data-leakage). Multiplicity correction is distinct from the magnitude of an improvement; see [statsmodels' multiple-testing reference](https://www.statsmodels.org/stable/generated/statsmodels.stats.multitest.multipletests.html).

## Evidence and recovery (证据与恢复)

Local `data/iteration/slot-v2.sqlite3` contains append-only, hash-checked studies, specifications, issues, outcomes, evaluations and releases. Old `data/iteration/daily_studies`, lifecycle records and all account ledgers are preserved. Registry decisions are rebuilt from evidence rather than trusted JSON flags. Source snapshots and inference parameters are checked again for accepted releases. The code never submits broker orders.

Back up source and SQLite databases before deployment. A reviewed implementation change is appended with exact prior/new fingerprints and test evidence; no prior predictions, orders or account states are rewritten. Roll back source and reviewed maintenance state together under the service locks, preserving any new evidence separately. Do not erase outcomes, reset attempts, or reopen an evaluated cohort to obtain another result.

## Verification (验证)

Tests cover sub-2% trials, separate slots, recent deterioration, per-horizon sample budgets, repeated-trial correction, immutable records, future/backdated data, missing cohort records, no early final-test peeking, trial withdrawal, source/config/code identity, and actual causal fitting with the original estimator. These software tests use isolated fixtures; only future collected market outcomes can establish independent model acceptance.
