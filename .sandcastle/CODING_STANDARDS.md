# Coding Standards

## Safety and domain rules

- Every broker-derived record, command, lock, event, and query is scoped to one `BrokerAccount`; reject cross-account references explicitly.
- Fail closed: missing, stale, ambiguous, incomplete, or unhealthy state must prevent exposure-increasing effects.
- Use UTC, immutable versions, and Decimal/NUMERIC for trading values. Do not use binary floating point for money, price, volume, or risk.
- MT5 is authoritative for Order, Fill, and Position. Timeout or ambiguous dispatch is `UNKNOWN`, never a blind retry or invented success.
- Tests, fixtures, and local development must never submit an order to a real broker. Production broker credentials must not enter source, logs, test output, or browser payloads.

## Architecture

- Keep the domain core deterministic and free of HTTP, database, WebSocket, LLM, and broker dependencies.
- Preserve the contracts in `docs/spec/`, `CONTEXT.md`, and their precedence rules. Do not weaken a safety contract to fit an implementation.
- Prefer narrow, explicit modules with clear ownership boundaries over generic abstractions.

## Testing

- Cover externally observable behavior and at least one relevant failure path for each changed safety boundary.
- Test account isolation, idempotency/fencing, stale data, and recovery whenever the changed behavior can affect them.
- Run `npm run typecheck` and `npm test` before committing.
