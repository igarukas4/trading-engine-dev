# Database migrations

Both Compose profiles run the ordered SQL migrations before starting the API. The `migrate` service uses the backend image and the PostgreSQL password file, so the password never enters a release environment value or process argument. A failed migration prevents the backend from starting.

`001_foundation.sql` contains migration bookkeeping and application metadata. `002_broker_account_connector.sql` adds immutable account identity, hash-only account-bound connector bindings, generation/lease state, read-only snapshots, and account-scoped audit records. `006_execution_safety.sql` stores account-local reservations, order intents, connector journals, fills, positions, fences, and operator commands. `009_discovery_pairing.sql` stores only expiring pairing state, salted device-code hashes, and candidate reports. It never stores device codes or connector secrets. `010_execution_coordination_checkpoint.sql` stores the coordinator restart checkpoint in PostgreSQL.
