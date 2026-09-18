# Connector delivery gate

The first connector command-delivery gate uses `backend/app/connector_delivery.py` as an
account-scoped in-memory transport registry. It owns one active authenticated session,
one ordered queue, and pending command/result state per BrokerAccount. This state is
lost on process restart and is **not** a substitute for the connector SQLite journal,
PostgreSQL-backed outbox, or durable dispatch stream. The future execution worker must
call the registry enqueue seam only after its durable intent/outbox transaction commits.

`UNKNOWN` is terminal for this in-memory delivery attempt. The registry never requeues
or blindly resends it; reconciliation/recovery must decide whether a later delivery is
safe after consulting durable journal and broker truth.
