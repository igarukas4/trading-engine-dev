# Database migrations

The development Compose profile runs migrations before starting the API. Migration files are ordered SQL and are applied by the `migrate` service with `psql`; production deployment can use the same files from its release job.

`001_foundation.sql` contains migration bookkeeping and application metadata. `002_broker_account_connector.sql` adds immutable account identity, hash-only account-bound connector bindings, generation/lease state, read-only snapshots, and account-scoped audit records. Order and execution commands remain intentionally absent from this slice.
