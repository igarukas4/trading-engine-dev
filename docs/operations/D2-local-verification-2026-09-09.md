# D2 — Local non-production verification evidence

**Window (UTC):** 2026-09-09T09:04:11Z–2026-09-09T09:14:31Z  
**Scope:** workstation-only review and deterministic fixture checks for D2. No VPS, DNS, broker, MT5 terminal, external credential, order, GitHub issue, or deployment was accessed or changed.

## Result

The full Node suite and every shell fixture passed in the existing Linux Sandcastle image. The container mounted the repository read-only; for shell fixtures only, it copied the tree into the container's temporary filesystem and normalized CRLF before execution. No source file or host state was altered by this validation.

`npm run typecheck` passed natively. The user-owned date-fixture edit in `tests/news-enrichment.test.mjs` was not changed.

## Commands and observed outcomes

| UTC | Command | Outcome |
| --- | --- | --- |
| 2026-09-09T09:04:11Z | `git status --short`; inspect `deploy/README.md`, `scripts/*.sh`, `tests/*deployment*.sh`, `tests/*release*.sh`, and `tests/*contract*.sh` | PASS — only pre-existing user changes were present; deployment controls and fixture coverage identified. |
| 2026-09-09T09:05Z | Git Bash: `tests/trusted-header-contract.sh` with a temporary in-process `python3` function targeting `C:\Python314\python.exe` | PASS — `trusted header contract passed`. It verifies proxy identity/forwarded-header overwrite semantics without a live backend. |
| 2026-09-09T09:06Z | Git Bash: smoke/release-transition/compose-project/release-environment regressions | BLOCKED — `smoke-release-regression.sh` rejects the temporary password file because Windows/MSYS cannot supply POSIX mode `0600`. No release script was run against a real Compose project. |
| 2026-09-09T09:08Z | `npm test` | BLOCKED — 21 Python-backed tests failed before product assertions because `python3` invokes the Microsoft Store stub (`status 9009`); 23 Node-only tests passed. |
| 2026-09-09T09:10Z | `npm run typecheck` | PASS. |
| 2026-09-09T09:12Z | `docker run --rm --entrypoint npm --mount type=bind,source=D:\CC\Trading Engine V0,target=/workspace --workdir /workspace sandcastle:trading-engine-v0 test` | PASS — 44 tests passed, 0 failed. |
| 2026-09-09T09:14Z | Sandcastle Linux container, read-only bind mount; temporary in-container copy with CRLF normalization; `for test_file in tests/*.sh; do bash "$test_file"; done` | PASS — all seven shell fixtures exited 0: `caddy-health-contract`, `compose-project-regression`, `deployment-contract`, `release-environment-regression`, `release-transition-regression`, `smoke-release-regression`, and `trusted-header-contract`. |

## Native Windows limitations (non-authoritative)

Native `npm test` was blocked before Python-backed assertions because `python3` resolves to the Microsoft Store stub (`status 9009`), while the installed interpreter is named `C:\Python314\python.exe`. Git Bash also cannot represent the fixture's required POSIX `0600` password-file mode. These workstation limitations do not apply to the passing Linux-container results above.

## What local fixtures cover

- Deployment file presence, digest pinning, private-network/no-port-publication rules, secret-file interfaces, log rotation, and rollback/backup/restore script contracts (`tests/deployment-contract.sh`).
- Trusted Caddy actor and forwarded-header overwrite behavior (passed above).
- Fixture-designed release failure recovery, rollback metadata swapping, sanitized Compose environment, smoke credential handling, and backup invocation (`tests/release-transition-regression.sh`, `tests/compose-project-regression.sh`, `tests/smoke-release-regression.sh`, `tests/release-environment-regression.sh`). These do not call a real VPS or broker.
- Domain fixtures exercise journaled `UNKNOWN`, reconciliation, emergency fences, connector loss/restart, and account isolation conceptually. They use in-process fake connectors; they do not prove MT5 or network behavior.

## Not provable locally; human gates remain

1. VPS firewall/DNS/TLS, immutable-image pull, actual Docker Compose health, authenticated public smoke check, rollback rehearsal, real backup plus ephemeral restore, off-host retention, and host monitoring/alerts.
2. DEMO MT5 login, connector credential storage and full-identity binding, handshake, reconciliation, market-data correctness, disconnect/restart behavior, and broker-authoritative `UNKNOWN` recovery.
3. Any DEMO command approval or order, including emergency-stop/explicit `close_all`, needs the designated human's explicit scoped authorization and must be observed in the real account. No LIVE action is in scope.

For the operator-side preparation and evidence template, use [D2-human-prerequisites.md](D2-human-prerequisites.md). This document records no secret values, tokens, passwords, account credentials, or endpoint URLs.
