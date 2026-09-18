# Direct MT5 Connector MVP Specification

**Status:** Implementation-ready specification
**Scope:** One Windows PC, one MT5 terminal, one DEMO account, `MANUAL` execution
**Protocol endpoint:** `/ws/v1/connector`
**Primary backend seam:** `backend/app/execution.py` `Broker`/connector adapter seam
**Authoritative domain contract:** [`backend-v0.md`](backend-v0.md)

## 1. Goal and scope

This specification defines the smallest verifiable direct MT5 connector slice. It is not a full connector platform and it does not replace the backend execution, risk, authorization, or reconciliation model.

The slice must allow an operator to:

1. run one connector process on the Windows PC where one MT5 terminal is installed;
2. bind that process to exactly one existing `BrokerAccount`;
3. connect outbound over WSS to `/ws/v1/connector`;
4. read the DEMO account, symbols, quotes, closed M1 candles, orders, deals, and positions;
5. execute one manually approved market order with native SL/TP after `order_check` passes;
6. modify native protection and close a position;
7. receive account-scoped acknowledgements and authoritative snapshots; and
8. reconnect and resolve an ambiguous command without blindly sending it again.

### 1.1 Included

- Windows-only runtime using the official `MetaTrader5` Python package.
- One process, one terminal, one DEMO `BrokerAccount`, one account queue/mutex.
- Account identity discovery and full-identity binding:
  `(provider, broker_server, external_account_id)`.
- Outbound WSS connection, authentication, heartbeat, reconnect, and snapshot synchronization.
- Read-only account, symbol, quote, closed M1, active-order, deal-history, and position reads.
- Market entry with native SL/TP.
- Native SL/TP modification.
- Position close, with an explicit position ticket and requested volume.
- A local SQLite journal with durable ambiguity handling.
- Fake-adapter unit tests, protocol contract tests, and DEMO smoke/acceptance tests.

### 1.2 Explicitly excluded

The implementation must not add or imply:

- LIVE execution or a LIVE unlock path;
- `SEMI_AUTO` or `FULL_AUTO` scheduling;
- multiple accounts or multiple MT5 terminals in one process;
- connector takeover/generation migration beyond rejecting stale generations;
- automatic credential discovery;
- strategy, news, AI, risk, or authorization logic on Windows;
- MT5 MCP, an AI agent, or a new HTTP trading API;
- automatic retry of an ambiguous broker side effect;
- automatic trailing ownership, close-all orchestration, or emergency fan-out;
- a second Windows-side domain model.

The backend remains authoritative for `BrokerAccount`, `Order`, `Fill`, `Position`, risk reservations, `Mode`, `BotState`, execution epochs, fences, and domain truth. MT5 remains authoritative for broker Order, Deal/Fill, Position, and native protection state.

### 1.3 Repository compatibility note

The current Windows checkout already contains the account-bound WSS endpoint, pairing/authentication primitives, heartbeat handling, reconciliation observation handling, and the injected execution seam. Its current authenticated stream is still a read-only foundation: the endpoint currently returns `snapshot`, accepts `heartbeat` and `reconciliation_observation`, and returns `READ_ONLY_FOUNDATION` for side-effecting messages.

Therefore implementation is two coordinated seams:

1. the connector must implement this document's protocol and adapter; and
2. the backend must add the server-to-connector command queue/dispatch handling before a live order test can pass.

Do not make the connector pretend that the present read-only endpoint can dispatch an order. The first integration gate must explicitly demonstrate command delivery is enabled.

## 2. Runtime architecture

```text
Windows PC
+-----------------------------------------------------------+
| MT5 terminal: terminal64.exe                             |
|   logged into one DEMO account                            |
|                                                           |
| mt5-connector process                                     |
|   Config/secret store                                     |
|   WSS client                                               |
|   AccountQueue (one mutex)                                 |
|   SQLite journal                                           |
|   MT5Adapter -> official MetaTrader5 Python package       |
+-----------------------------|-----------------------------+
                              | outbound WSS only
                              v
Lighthouse VPS backend: /ws/v1/connector
  BrokerAccount + ExecutionEngine + PostgreSQL/outbox
```

### 2.1 Process rules

- The connector is a long-running foreground process during MVP testing. A Windows service wrapper may be added only after foreground acceptance passes.
- The connector owns one `MetaTrader5` module connection and one account queue. No two MT5 calls run concurrently.
- All outbound messages are account-scoped. The process must fail closed if a message names any other account or identity.
- The connector must tolerate MT5 terminal restarts by marking itself unhealthy, calling `shutdown()`, and reinitializing with bounded backoff.
- Process restart never resumes side effects from memory. It loads the SQLite journal first, marks pre-invocation work as not invoked, leaves post-invocation work `UNKNOWN`, reconnects, and reconciles before accepting a new side effect.
- The connector must log structured event names and safe identifiers only. Secrets, account passwords, WSS query strings containing credentials, and full raw credential-bearing payloads must never be logged.

### 2.2 Suggested implementation shape

The exact directory is not normative, but the implementation should remain a small deep module behind the backend connector seam:

```text
connector/
├── pyproject.toml
├── src/mt5_connector/
│   ├── main.py
│   ├── config.py
│   ├── models.py
│   ├── websocket_client.py
│   ├── mt5_adapter.py
│   ├── journal.py
│   ├── dispatcher.py
│   └── reconciliation.py
└── tests/
    ├── test_journal.py
    ├── test_dispatcher.py
    ├── test_protocol.py
    ├── test_reconciliation.py
    └── test_mt5_adapter_fake.py
```

The public seam should expose a small `BrokerAdapter` interface. `MetaTrader5` namedtuples, retcodes, and IPC exceptions must not leak into backend domain objects.

## 3. Account binding and readiness

### 3.1 Binding identity

The connector configuration contains:

- `account_id`: backend `BrokerAccount.id`;
- `provider`: `MT5`;
- `broker_server`: expected MT5 server string;
- `external_account_id`: expected MT5 login/account identifier;
- `key_id` and connector secret provisioned through the existing backend binding flow;
- MT5 terminal executable path, defaulting to `C:\Program Files\MetaTrader 5\terminal64.exe`;
- local journal path; and
- WSS URL, which must use `wss://` outside an explicitly local test.

After `initialize()`, the adapter reads `account_info()` and derives the actual login and server. The connector refuses the session if any member of the configured full identity differs. It must not silently rebind an account to another broker server or login.

### 3.2 First-order gate

The first DEMO order is allowed only when all conditions below are true:

- the operator has explicitly provisioned a `DEMO` `BrokerAccount` in the backend;
- backend account mode is `MANUAL`, and the operator has separately approved the specific order;
- the connector secret is loaded from Windows protected credential storage or an equivalent ACL-protected secret file;
- the MT5 terminal is initialized and `account_info()` matches the full configured identity;
- `trade_allowed` and terminal trading permission are true;
- the WSS session is authenticated, current-generation, and within its heartbeat lease;
- the backend has completed the initial authoritative snapshot and has no unresolved `UNKNOWN` command for this account;
- the account has a valid Pair-to-MT5-symbol mapping and the symbol is visible/selectable;
- the request has fresh backend risk approval, current `execution_epoch`, current dispatch sequence, and a unique idempotency key;
- the market order includes both native `sl` and native `tp`, quantized to the symbol digits and valid stop/freeze distances;
- the local journal is writable and has successfully committed `RECEIVED` before any MT5 call; and
- the connector account mutex is free and no reconciliation/fence gate is active.

A connector must reject an execution command locally when these conditions are not present. Local rejection is not a substitute for backend authorization; it is an additional fail-closed check.

## 4. WSS connector interface

### 4.1 Transport

The Windows process opens an outbound connection to:

```text
wss://<backend-host>/ws/v1/connector
```

Credentials are sent in the authenticated handshake body, never in the URL, query string, process command line, or log output. TLS certificate verification is mandatory. The connector does not accept inbound HTTP trading requests.

### 4.2 Exact opening message

The first frame is the following JSON object. It must contain exactly these fields; no extra fields are permitted in the MVP handshake:

```json
{
  "type": "hello",
  "account_id": "broker-account-uuid",
  "provider": "MT5",
  "broker_server": "MetaQuotes-Demo",
  "external_account_id": "12345678",
  "key_id": "binding-key-id",
  "secret": "provisioned-secret",
  "generation": 1,
  "session_id": "windows-pc-terminal-session-uuid"
}
```

`generation` is the durable backend connector generation. The connector must persist the accepted generation locally and reject backend command frames for another generation. The connector must never invent a new generation.

The current backend sends the first authenticated response as:

```json
{
  "type": "snapshot",
  "snapshot": { "account_id": "...", "identity": {}, "connector": {}, "reconciliation": {} }
}
```

The connector must accept this response shape. The command-enabled backend may add `generation`, `server_sequence`, and `execution_epoch` to the response; those fields become mandatory once the command protocol is enabled.

A failed authentication or identity mismatch closes the session with a non-success WebSocket close and a safe error code such as `WRONG_ACCOUNT`, `STALE_GENERATION`, or `AUTHENTICATION_FAILED`. The connector must not retry a bad credential indefinitely; it enters operator-action-required state.

### 4.3 Common post-handshake envelope

Every post-handshake frame has these fields:

```json
{
  "schema_version": 1,
  "type": "heartbeat",
  "message_id": "message-uuid",
  "account_id": "broker-account-uuid",
  "provider": "MT5",
  "broker_server": "MetaQuotes-Demo",
  "external_account_id": "12345678",
  "generation": 1,
  "sequence": 17,
  "execution_epoch": 4,
  "command_id": null,
  "idempotency_key": "msg:message-uuid",
  "sent_at": "2026-09-18T00:00:00Z",
  "payload": {}
}
```

Rules:

- `sequence` is strictly increasing per sender direction and generation. Client and server maintain separate sequences.
- `message_id` is unique within the account/generation and is retained for replay detection.
- `account_id`, the three identity fields, and `generation` must match the authenticated session.
- `execution_epoch` is the backend epoch observed by the connector. Exposure-increasing commands must match the current backend epoch immediately before invocation.
- `command_id` is non-null for command requests and command results. It is null for telemetry/control frames.
- `idempotency_key` is required on every frame. For telemetry it is `msg:<message_id>`; for a command it is the backend-provided command key and must be echoed unchanged in all results.
- A connector must reject out-of-order, replayed, wrong-account, wrong-generation, and wrong-epoch side-effect frames. The backend must enforce the same checks rather than relying on the connector alone.

### 4.4 Heartbeat

The connector sends a heartbeat every 10 seconds and expects an acknowledgement within 10 seconds. The backend lease is 30 seconds. A heartbeat is:

```json
{
  "schema_version": 1,
  "type": "heartbeat",
  "message_id": "message-uuid",
  "account_id": "broker-account-uuid",
  "provider": "MT5",
  "broker_server": "MetaQuotes-Demo",
  "external_account_id": "12345678",
  "generation": 1,
  "sequence": 18,
  "execution_epoch": 4,
  "command_id": null,
  "idempotency_key": "msg:message-uuid",
  "sent_at": "2026-09-18T00:00:10Z",
  "payload": {
    "terminal_connected": true,
    "last_mt5_error": null,
    "journal_state": "READY"
  }
}
```

The current backend compatibility form is `{"type":"heartbeat","account_id":"...","generation":1,"session_id":"..."}` and receives `heartbeat_ack`. Support it until the envelope upgrade is deployed, but do not use the compatibility form for side-effecting commands.

### 4.5 Connector-to-backend read/event frames

The MVP connector sends these event types:

- `account_snapshot`: balance, equity, currency, leverage, trade permissions, orders, deals, positions, and journal states.
- `market_snapshot`: requested symbol metadata and current bid/ask observations.
- `quote`: one account-scoped symbol quote, if the backend requests/polls it.
- `candle_batch`: closed M1 candles only; each record has symbol, UTC open time, OHLC, tick volume, real volume, and spread.
- `reconciliation_observation`: orders, deals, positions, and command matches after reconnect or ambiguity.
- `command.result`: terminal result for a command.
- `error`: safe protocol/adapter error with a stable code.

An `account_snapshot` payload has this minimum shape:

```json
{
  "observed_at": "2026-09-18T00:00:00Z",
  "sequence_watermark": 42,
  "account": {
    "login": "12345678",
    "server": "MetaQuotes-Demo",
    "currency": "USD",
    "leverage": 100,
    "balance": "10000.00",
    "equity": "10000.00",
    "trade_allowed": true,
    "margin_mode": "HEDGING"
  },
  "orders": [],
  "deals": [],
  "positions": [],
  "journal": [],
  "gaps": []
}
```

Every broker-derived order, deal, position, gap, and journal entry includes `account_id`. External tickets are strings at the wire boundary and are unique only within this account.

### 4.6 Backend-to-connector command frames

The command-enabled backend sends commands in the common envelope with non-null `command_id` and `idempotency_key`. The MVP command types are:

#### `account_snapshot.request`

Read-only. Payload:

```json
{ "include": ["orders", "deals", "positions"], "deal_cursor": null }
```

#### `market_snapshot.request`

Read-only. Payload:

```json
{ "symbols": ["EURUSD.a"], "include_symbol_info": true, "include_ticks": true }
```

#### `candle_batch.request`

Read-only. Payload:

```json
{ "symbol": "EURUSD.a", "timeframe": "M1", "closed_only": true, "count": 200 }
```

`closed_only=true` is mandatory for the MVP. The current MT5 bar at index 0 is never returned as a closed candle.

#### `order.submit_market`

Side-effecting. Payload:

```json
{
  "symbol": "EURUSD.a",
  "direction": "LONG",
  "volume": "0.01",
  "sl": "1.07000",
  "tp": "1.08000",
  "deviation_points": 20,
  "magic": 26091801,
  "comment": "te:command-uuid"
}
```

The connector derives the MT5 order type and current ask/bid. It must not accept a caller-supplied price as authority for a market order. Native `sl` and `tp` are mandatory and must be present in the actual `order_send` request.

#### `position.modify_protection`

Side-effecting. Payload:

```json
{
  "position_ticket": "123456789",
  "symbol": "EURUSD.a",
  "sl": "1.07000",
  "tp": "1.08000",
  "expected_position_version": 7
}
```

#### `position.close`

Side-effecting and reduce-only. Payload:

```json
{
  "position_ticket": "123456789",
  "symbol": "EURUSD.a",
  "direction": "LONG",
  "volume": "0.01",
  "expected_position_volume": "0.01",
  "deviation_points": 20,
  "magic": 26091801,
  "comment": "te:command-uuid"
}
```

The connector reads the fresh position immediately before invoking MT5 and rejects a request whose ticket, symbol, direction, or available volume no longer matches. It never sends a close volume greater than the fresh position volume.

#### `reconcile.request`

Read-only. Payload:

```json
{
  "reason": "RECONNECT | UNKNOWN_COMMAND | OPERATOR_REQUEST",
  "command_ids": ["command-uuid"],
  "from_server_time": "2026-09-17T23:55:00Z"
}
```

The connector returns a complete account snapshot plus an explicit match result for every requested command. It does not send an order as part of reconciliation.

### 4.7 Command result

The connector returns one `command.result` for every accepted command frame, including local rejection:

```json
{
  "schema_version": 1,
  "type": "command.result",
  "message_id": "result-message-uuid",
  "account_id": "broker-account-uuid",
  "provider": "MT5",
  "broker_server": "MetaQuotes-Demo",
  "external_account_id": "12345678",
  "generation": 1,
  "sequence": 19,
  "execution_epoch": 4,
  "command_id": "command-uuid",
  "idempotency_key": "manual-order-001",
  "sent_at": "2026-09-18T00:00:01Z",
  "payload": {
    "state": "ACCEPTED",
    "code": "TRADE_RETCODE_DONE",
    "retcode": 10009,
    "external_order_id": "987654321",
    "external_deal_id": "987654322",
    "position_tickets": ["987654321"],
    "protection_status": "UNCONFIRMED",
    "snapshot": { "orders": [], "deals": [], "positions": [] }
  }
}
```

`state` is one of `ACCEPTED`, `REJECTED`, or `UNKNOWN`. A result is not a permission to retry. **No blind retry is allowed.** For an entry, `ACCEPTED` means MT5 returned an accepted/completed/partial retcode; `protection_status` remains `UNCONFIRMED` until a subsequent snapshot proves the requested native SL/TP. The backend keeps the account exposure gate closed until that confirmation.

## 5. Command state machine and ordering

### 5.1 Connector-local state machine

```text
RECEIVED ──pre-invocation validation failure──▶ REJECTED
    │
    └─ durable INVOKING immediately before side-effecting MT5 call
          ├─ known broker result ─────────────▶ ACCEPTED or REJECTED
          └─ timeout/crash/transport ambiguity▶ UNKNOWN
```

`RECEIVED`, `INVOKING`, and the terminal result are journal records, not merely in-memory labels. The implementation may store a separate `phase` such as `CHECKING` or `RECONCILING`, but the externally visible state is one of the five states above.

### 5.2 Exact transition rules

| Current state | Event | Next state | Required behavior |
|---|---|---|---|
| absent | valid command received | `RECEIVED` | Begin a SQLite transaction, insert the full command and request hash, commit/fsync before any MT5 call. |
| `RECEIVED` | validation, epoch, fence, or symbol check fails | `REJECTED` | No `order_check` or `order_send`; record stable rejection code. |
| `RECEIVED` | `order_check` starts | `INVOKING` phase=`CHECKING` | Persist the phase before the check. |
| phase=`CHECKING` | nonzero/invalid check result | `REJECTED` | Never call `order_send`. |
| phase=`CHECKING` | check passes | phase=`CHECKED` | Persist the passed check and request hash. |
| phase=`CHECKED` | immediately before `order_send`/modify/close | `INVOKING` phase=`DISPATCHING` | This fsync is the ambiguity boundary. |
| `INVOKING` | reliable accepted MT5 result | `ACCEPTED` | Persist retcode/tickets and publish `command.result`. |
| `INVOKING` | reliable rejected MT5 result | `REJECTED` | Persist retcode and publish `command.result`. |
| `INVOKING` | timeout, IPC loss, process crash, missing result, or uncertain transport | `UNKNOWN` | Never resend. Publish `UNKNOWN` when connected again and reconcile first. |
| `UNKNOWN` | unique broker evidence of accepted effect | `ACCEPTED` | Record match and snapshot; do not invoke MT5. |
| `UNKNOWN` | complete authoritative evidence of no effect/rejection | `REJECTED` | Record reconciliation proof; do not invoke MT5. |
| `UNKNOWN` | incomplete or conflicting evidence | `UNKNOWN` | Keep the account quarantined and require further reconciliation/operator action. |
| terminal state | duplicate same command and same request hash | unchanged | Return the stored result; never call MT5 again. |
| any | same idempotency key with a different request hash | unchanged | Reject with `IDEMPOTENCY_KEY_REUSED`; never call MT5. |

A process crash after `RECEIVED` but before `INVOKING` is resolved on restart as `REJECTED` with `NOT_INVOKED_AFTER_RESTART` (the backend may map this to its richer `ABORTED_NOT_INVOKED` state). A crash after `INVOKING` is always `UNKNOWN`.

### 5.3 Account queue/mutex

- Commands for this account are processed strictly in backend dispatch sequence order.
- The connector holds one account mutex across validation, MT5 invocation, immediate read-back, and journal transition.
- It never starts a second MT5 call concurrently with an existing call.
- A missing sequence, stale epoch, stale generation, or blocked safety gate stops the queue and triggers reconciliation; it does not permit out-of-order execution.
- Reconnect does not clear an `UNKNOWN` state or advance past it. The backend must resolve the unknown command before delivering a later side-effecting command.

## 6. Local SQLite journal

### 6.1 Required schema

SQLite must run in WAL mode with `PRAGMA synchronous=FULL`, foreign keys enabled, and a single writer protected by the account mutex. The minimum schema is:

```sql
CREATE TABLE IF NOT EXISTS connector_commands (
    account_id TEXT NOT NULL,
    command_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    dispatch_sequence INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    execution_epoch INTEGER NOT NULL,
    command_type TEXT NOT NULL,
    request_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('RECEIVED','INVOKING','ACCEPTED','REJECTED','UNKNOWN')),
    phase TEXT NOT NULL DEFAULT 'RECEIVED',
    mt5_invoked INTEGER NOT NULL DEFAULT 0,
    retcode INTEGER,
    external_order_id TEXT,
    external_deal_id TEXT,
    external_position_ids_json TEXT NOT NULL DEFAULT '[]',
    result_json TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT,
    PRIMARY KEY (account_id, command_id),
    UNIQUE (account_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS connector_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    command_id TEXT,
    observed_at TEXT NOT NULL,
    source TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    match_status TEXT,
    UNIQUE (account_id, command_id, observed_at, source)
);

CREATE TABLE IF NOT EXISTS connector_meta (
    account_id TEXT PRIMARY KEY,
    generation INTEGER NOT NULL,
    next_client_sequence INTEGER NOT NULL,
    last_server_sequence INTEGER NOT NULL,
    last_reconciliation_watermark TEXT,
    updated_at TEXT NOT NULL
);
```

`command_id` and `idempotency_key` are unique within the account. The connector must not use a global uniqueness assumption across accounts.

### 6.2 Durable write points

1. Insert `RECEIVED` and commit before validation that can call MT5, and before any side-effecting operation.
2. Persist `phase=CHECKING` before `order_check`.
3. Persist `phase=CHECKED` only after a successful check result.
4. Persist `state=INVOKING`, `phase=DISPATCHING`, and `mt5_invoked=1` in one commit immediately before `order_send`, protection modification, or close.
5. Persist the returned retcode/tickets and terminal state before publishing the WSS result.
6. Persist every reconciliation observation before using it to resolve an `UNKNOWN` command.

The journal is the connector's lifecycle truth across process restart. MT5 remains broker truth; the journal never fabricates a broker result.

### 6.3 Restart recovery and retention

At startup:

1. verify the SQLite database and journal integrity;
2. load the current account/generation and reject a stale local generation;
3. convert `RECEIVED`/`CHECKING`/`CHECKED` rows with `mt5_invoked=0` to `REJECTED` with `NOT_INVOKED_AFTER_RESTART`;
4. leave every `INVOKING` row with `mt5_invoked=1` as `UNKNOWN`;
5. connect and send a complete reconciliation request before accepting side effects; and
6. retain all unresolved `UNKNOWN` rows and their evidence.

Resolved command rows and observations may be compacted after 30 days. `UNKNOWN` rows, their request payload/hash, and their resolution evidence are retained until backend acknowledgement and then for at least the normal audit retention period. Journal cleanup must never delete the only proof used to prevent a duplicate send.

## 7. MT5 adapter contract

### 7.1 Initialization and health

Use the official `MetaTrader5` package on Windows. Call `mt5.initialize(path=..., timeout=60000)` with the configured terminal path. Do not pass account passwords unless the operator explicitly provisions that configuration; the terminal's existing logged-in session is the default MVP path.

Health requires all of:

- `initialize()` returns true;
- `terminal_info()` is available;
- `version()` is available;
- `account_info()` is non-null;
- the actual login/server match the configured identity; and
- the terminal is allowed to trade for the DEMO smoke test.

On failed calls, capture `mt5.last_error()` as a structured safe code. IPC/terminal errors, `None` results, and lost connection are transport failures; they do not prove a broker-side order was rejected after `order_send`.

### 7.2 Symbol, quote, and candle reads

- `symbol_info(symbol)` supplies digits, point, volume min/max/step, trade/stops/freeze levels, filling modes, and visibility.
- If a configured symbol is not visible, call `symbol_select(symbol, True)` and verify the result. Do not invent a broker symbol from the canonical Pair string.
- `symbol_info_tick(symbol)` supplies current bid/ask and tick time.
- `copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 1, count)` is the closed-M1 read. Index 0 is the current/open bar and must not be emitted as closed.
- `orders_get()` returns active pending orders.
- `positions_get()` returns open positions.
- `history_deals_get(from_datetime, to_datetime)` and the relevant order-history function provide overlapping deal/order reconciliation windows. The server-time window must cover the command invocation time with a safety margin.
- Convert all timestamps to UTC ISO-8601 at the wire boundary. Preserve raw broker ticket IDs as strings.
- Return `None`/error distinctly from an empty result. An empty list is valid only after the adapter has established that the query succeeded.

### 7.3 Request construction

All prices and volumes are parsed as `Decimal` in the connector model, quantized using symbol metadata, and converted only at the MT5 call boundary. The request builder must validate:

- symbol visibility and trading availability;
- volume min/max/step;
- price/sl/tp digit quantization;
- direction and order type;
- stop and freeze levels;
- requested native SL/TP presence for entries;
- allowed filling mode; and
- `trade_allowed`.

For a market entry, construct `TRADE_ACTION_DEAL` with `ORDER_TYPE_BUY`/`ORDER_TYPE_SELL`, current ask/bid, volume, native `sl`, native `tp`, `deviation`, `magic`, bounded `comment`, `ORDER_TIME_GTC`, and a symbol-supported `type_filling`.

For protection modification, construct `TRADE_ACTION_SLTP` with the exact position ticket and requested `sl`/`tp`.

For a position close, construct the opposite market direction with the exact `position` ticket, fresh current price, requested volume, `reduce_only` enforced by the connector's fresh-volume check, and a correlation comment. A hedging close must never omit the ticket.

### 7.4 Check/send behavior

- Call `order_check(request)` first for every entry, modification, and close.
- Treat the documented successful check result (`retcode == 0`) as pass; any other result is `REJECTED` and `order_send` is prohibited.
- Persist `INVOKING` immediately before `order_send`, `order_send` for `TRADE_ACTION_SLTP`, or the close `order_send`.
- Read back the relevant order/deal/position snapshot after a reliable accepted response, but do not turn a missing immediate position into a retry.
- Native SL/TP is mandatory for new exposure. A position is not marked safely protected until a read-back confirms acceptable broker-native values.

### 7.5 Retcode mapping

Use named `MetaTrader5` constants when available and preserve the numeric retcode in results. The minimum mapping is:

- `ACCEPTED`: `TRADE_RETCODE_PLACED` (10008), `TRADE_RETCODE_DONE` (10009), `TRADE_RETCODE_DONE_PARTIAL` (10010). Partial results include actual filled volume and remain open for reconciliation.
- `REJECTED`: explicit, reliable broker decisions such as `REJECT` (10006), `CANCEL` (10007), `ERROR` (10011), `INVALID` (10013), invalid volume/price/stops (10014–10016), disabled/closed/no money (10017–10019), invalid expiration (10022), no changes (10025), client/server autotrading disabled (10026–10027), frozen (10029), invalid filling (10030), invalid order (10035), and other explicit terminal rejections.
- `UNKNOWN`: `order_send` raises, returns `None`, the terminal connection disappears, the call times out, the process crashes after `INVOKING`, or the result is otherwise not trustworthy. `TRADE_RETCODE_TIMEOUT` (10012) is treated as ambiguous after invocation. The connector must reconcile rather than resend.

Retcode classification is conservative. When the connector cannot prove that MT5 did not receive the side effect, it uses `UNKNOWN`.

## 8. Security and safe defaults

- Store the connector secret using Windows Credential Manager or a mode/ACL-restricted file outside the repository. Do not store it in `.env` committed to Git.
- The secret must never appear in URLs, command lines, crash dumps intentionally emitted by the connector, logs, screenshots, test fixtures, or WSS error payloads.
- The backend stores only a salted hash and `key_id`; the connector receives the secret only through the authenticated provisioning flow.
- Bind the session to the complete immutable identity, not only `account_id` or only login number.
- Require WSS certificate verification and reject plaintext WSS downgrade.
- Start with `DEMO`, `MANUAL`, and execution disabled until the explicit operator preflight passes. There is no LIVE configuration branch in this MVP.
- Do not trust `actor`, user name, or authorization claims from connector payloads. The backend supplies authorization and approval.
- Redact `password`, `secret`, `key`, and credential-storage values recursively in structured logs.
- Use a bounded message size, bounded snapshot size, and rate limit. Reject malformed JSON and unknown command types without touching MT5.
- The connector must not expose a listening HTTP port. The only network connection is the outbound WSS session.

## 9. Reconnection and reconciliation

### 9.1 Reconnect policy

On a normal disconnect or heartbeat timeout:

1. stop accepting new side-effecting commands;
2. keep the last confirmed MT5 native protection in place;
3. mark the session unhealthy and retain the current generation;
4. reconnect with exponential backoff of 1, 2, 4, 8, 16, 32, then 60 seconds, with bounded jitter;
5. authenticate with the same `account_id`, identity, key, generation, and a new `session_id`; and
6. complete reconciliation before accepting another side effect.

A stale-generation response is terminal for that session. It must not be solved by inventing a generation locally.

### 9.2 Reconciliation sequence

After every reconnect, restart, `UNKNOWN`, or detected sequence gap:

1. request/read account identity and permissions;
2. read active orders with `orders_get()`;
3. read open positions with `positions_get()`;
4. read an overlapping server-time window of orders and deals;
5. read the local journal rows for the affected generation/commands;
6. match broker effects first by durable correlation (`magic` plus bounded comment/correlation token), then by MT5 order/deal/position relationships;
7. use symbol, direction, volume, price, and time only as supporting evidence, never as the sole match;
8. emit a complete `reconciliation_observation` and account snapshot; and
9. advance the reconciliation watermark only after all requested collections are complete and no gap remains.

### 9.3 Clearing `UNKNOWN`

An `UNKNOWN` command may leave `UNKNOWN` only when one of these proofs exists:

- exactly one broker order/deal/position effect matches the command correlation and the resulting state is accepted/filled/partially filled; or
- the complete overlapping history and current snapshot provide authoritative evidence that the broker rejected/cancelled the request or that no broker invocation occurred.

If more than one effect matches, the history window is incomplete, the correlation is absent, or current state conflicts with the journal, the command remains `UNKNOWN`, the account stays quarantined for new exposure, and an operator alert is emitted.

The connector never automatically issues a replacement order for an `UNKNOWN` command. A later manual command is permitted only after the backend has recorded the old command's terminal reconciliation result, projected all fills/positions, confirmed native protection, cleared the account fence, and obtained a new explicit approval/idempotency key.

This is the duplicate-exposure proof: an ambiguous order is either linked to the one broker effect already present or remains blocked; reconnect recovery never calls `order_send` again for the same command.

## 10. Tests and acceptance criteria

### 10.1 Unit tests with a fake MT5 adapter

The fake adapter must prove:

- account identity mismatch is rejected before trading;
- closed-candle reads start at index 1 and preserve UTC/schema;
- invalid volume, price, stops, filling mode, and missing native SL/TP are rejected;
- failed `order_check` never calls `order_send`;
- successful market entry includes native `sl` and `tp` in the exact request;
- protection modification uses `TRADE_ACTION_SLTP` and exact position ticket;
- close uses the opposite direction and never exceeds fresh position volume;
- an explicit accepted retcode becomes `ACCEPTED`;
- an explicit rejection becomes `REJECTED`;
- exception, timeout, `None`, and ambiguous transport result become `UNKNOWN`;
- duplicate same-hash commands return the stored result with zero additional MT5 calls; and
- a different request with the same idempotency key is rejected with zero MT5 calls.

### 10.2 Journal/restart tests

- Kill/restart after `RECEIVED` and before invocation: row resolves to `REJECTED/NOT_INVOKED_AFTER_RESTART` and no MT5 call occurs.
- Kill immediately after the journal commits `INVOKING`: row remains/resolves to `UNKNOWN` and no automatic resend occurs.
- Journal corruption or unwritable path blocks execution and reports a safe health error.
- Resolved rows can be retained/compacted without deleting unresolved evidence.

### 10.3 WSS contract tests

Capture and test exact JSON frames for:

- the `hello` opening message;
- legacy current-backend `snapshot` response;
- common envelope validation;
- heartbeat and heartbeat acknowledgement;
- wrong account, wrong identity, stale generation, replayed sequence, and stale epoch;
- account snapshot, market snapshot, closed-candle batch, command result, and reconciliation observation;
- command idempotency and duplicate result replay; and
- malformed/unknown commands returning an error without MT5 invocation.

The tests must run without a live terminal and must assert that secrets do not appear in serialized errors or logs.

### 10.4 DEMO smoke and manual execution tests

On the verified Windows PC and an explicitly selected DEMO account:

1. verify `terminal64.exe` is running and the terminal is logged into the expected DEMO server/login;
2. install the pinned `MetaTrader5` package in the connector virtual environment;
3. run connector health and identity discovery;
4. connect to the backend and verify the account-scoped snapshot;
5. request a known symbol quote and at least 10 closed M1 candles;
6. verify active orders, recent deals, and positions are returned as account-scoped data;
7. configure a small DEMO market order with explicit native SL/TP and obtain separate manual approval;
8. verify `order_check` passes and prove `order_send` was called exactly once;
9. verify the MT5 result, order/deal/position snapshot, and native SL/TP read-back;
10. modify native protection and verify the position snapshot changes; and
11. close the position by its exact ticket and verify no open exposure remains.

The evidence must include timestamps, command IDs, idempotency keys, retcodes, external IDs, and redacted logs. Never use a LIVE account for this test.

### 10.5 Reconnect and ambiguity tests

- Disconnect the WSS session after a clean snapshot; verify reconnect, heartbeat, full snapshot, and no duplicate side effect.
- Drop the connection after `INVOKING` is durably written and make the fake adapter report one broker fill; verify reconciliation marks the original command accepted and `order_send` count remains one.
- Drop the connection after `INVOKING` and make the fake adapter report no effect only after a complete history window; verify the original command is resolved as rejected/not-found and no resend occurs.
- Return two plausible matches; verify the command remains `UNKNOWN`, the account remains fenced, and no resend occurs.
- Deliver a stale generation or replayed command after reconnect; verify rejection and zero MT5 calls.

### 10.6 Acceptance gate

The connector MVP is not accepted until all of the following are true:

- all unit and protocol tests pass;
- the current backend read-only compatibility handshake passes;
- the command-enabled backend contract test proves server-to-connector delivery;
- the DEMO read-only smoke test passes;
- one manually approved DEMO market order with native SL/TP passes and is read back from MT5;
- native protection modification and exact-ticket close pass;
- restart, reconnect, crash-after-invocation, and ambiguous-reconciliation tests pass; and
- the duplicate-exposure test proves the same ambiguous command invokes MT5 at most once.

## 11. Operational runbook

### Start

1. Confirm the Windows MT5 terminal is running and logged into the intended DEMO account.
2. Confirm the connector configuration points to that exact terminal/server/login.
3. Confirm the local journal path is writable and the secret is available from protected storage.
4. Start the connector in foreground mode with execution disabled.
5. Verify identity, WSS authentication, heartbeat, and complete snapshot.
6. Enable the explicitly approved MANUAL test path only after the first-order gate passes.

### Stop safely

1. Stop accepting new backend commands.
2. Wait for the queue to become idle.
3. If any command is `INVOKING` or `UNKNOWN`, do not delete the journal or kill the process without recording the crash test condition.
4. Keep MT5 native SL/TP active.
5. Stop the connector and leave the account blocked until the next complete reconciliation.

### Inspect health without exposing secrets

Inspect:

- process/terminal connectivity;
- account login/server match;
- WSS generation and heartbeat age;
- journal counts by state;
- last reconciliation watermark;
- last safe MT5 error code; and
- active orders/positions by external ticket.

Never print the handshake secret, MT5 password, full credential-store payload, or WSS URL containing credentials.

### Recovery after connector crash

1. Do not resend the last command from a shell or operator script.
2. Restart the connector with the same account and generation.
3. Let journal startup classify pre-invocation and post-invocation rows.
4. Complete full order/deal/position reconciliation.
5. Resolve each `UNKNOWN` through the backend recovery path.
6. Confirm native protection and clear the account fence only after convergence.
7. Require a new explicit manual approval for any subsequent order.

### Disable execution safely

Set the connector to read-only/disabled mode, stop delivery of new side-effecting commands, and keep monitoring/reconciliation enabled. Do not close positions automatically as part of connector shutdown. Existing MT5-native SL/TP remains the broker protection while the connector is offline.

## 12. Implementation completion questions

The coding agent must be able to answer these without inventing behavior:

- **What exact message opens a connector session?** The `hello` object in §4.2, with exactly nine fields.
- **How is one account bound to one session?** The authenticated `account_id`, full immutable identity, key binding, current generation, and one session/queue; any mismatch is rejected.
- **What fields identify a command?** Account ID, command ID, generation, dispatch sequence, execution epoch, idempotency key, canonical request hash, and command payload.
- **When is a command journaled?** `RECEIVED` is committed before any MT5 call; `INVOKING`/`DISPATCHING` is committed immediately before the side-effecting call.
- **What happens if `order_send` times out?** The command becomes `UNKNOWN`; it is not resent.
- **What happens after a crash immediately after `order_send`?** The durable `INVOKING` record is recovered as `UNKNOWN` and reconciled against MT5 orders, deals, and positions.
- **How is ambiguity resolved?** Journal-first, then complete overlapping MT5 history/current snapshots; match durable correlation and broker relationships, never only symbol/volume/price/time.
- **Which events go back to the backend?** Heartbeat, account/market snapshots, closed-candle batches, reconciliation observations, command results, and safe errors.
- **Which actions are allowed?** Read account/market/order/deal/position state, market entry with native SL/TP, native protection modification, and exact-ticket position close, all for one DEMO account in MANUAL mode.
- **What proves duplicate exposure cannot result from reconnect recovery?** Same-command idempotency plus journal state, single account mutex, `INVOKING` ambiguity boundary, no-blind-retry rule, and acceptance tests showing one ambiguous command produces at most one MT5 invocation.
- **What must be true before the first DEMO order?** Every first-order gate in §3.2, including explicit manual approval, current epoch/generation, complete reconciliation, writable journal, passing `order_check`, and native SL/TP in the actual request.

## References

- Repository backend contract: `docs/spec/backend-v0.md`
- Domain vocabulary: `CONTEXT.md`
- Existing Windows connector research: `docs/research/mt5-connector.md` on the research branch
- Official `initialize`: <https://www.mql5.com/en/docs/python_metatrader5/mt5initialize_py>
- Official `order_check`: <https://www.mql5.com/en/docs/python_metatrader5/mt5ordercheck_py>
- Official `order_send`: <https://www.mql5.com/en/docs/python_metatrader5/mt5ordersend_py>
- Official `copy_rates_from_pos`: <https://www.mql5.com/en/docs/python_metatrader5/mt5copyratesfrompos_py>
- Official `positions_get`: <https://www.mql5.com/en/docs/python_metatrader5/mt5positionsget_py>
- Official `orders_get`: <https://www.mql5.com/en/docs/python_metatrader5/mt5ordersget_py>
- Official trade return codes: <https://www.mql5.com/en/docs/constants/errorswarnings/enum_trade_return_codes>
