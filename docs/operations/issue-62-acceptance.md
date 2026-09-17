# Issue 62 acceptance

The automated acceptance harness is `tests/operational-convergence.test.mjs`.
It covers:

- forward-only migration admission, including refusal when the database has a
  migration that the image does not contain;
- restart fencing until a complete, account-bound broker snapshot is applied;
- durable recovery state in account dashboard snapshots;
- three independent DEMO/LIVE-labelled accounts with isolated connector
  outcomes and one-account quarantine;
- global stop-only membership and unresolved-target persistence across restart.

The production migration container must complete before the backend starts. A
newer database schema therefore blocks the image instead of attempting a
downgrade. Recovery of an account never reopens entry until that account's
connector supplies a complete snapshot of orders, fills, and positions.

## Human-only DEMO/MT5 checks

The fake connector cannot prove MT5 transport behavior. An operator must still
run these checks with three separate terminal sessions:

1. Pair two DEMO accounts and one LIVE account, then verify each connector is
   bound to exactly one broker identity and generation.
2. Interrupt one connector during an entry request. Confirm that account stays
   fenced or quarantined, while the other two accounts continue their own
   monitoring and dispatch.
3. Restart the backend while one account has an unresolved broker effect.
   Confirm the affected account remains blocked until its full MT5 snapshot is
   reconciled.
4. Run stop-only and explicit close-all as separate global operations. Leave
   one terminal offline and verify the operation remains unresolved until that
   target converges.

These checks require the custodian's normal LIVE authorization and must not be
replaced with a test trade or automatic promotion gate.
