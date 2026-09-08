# Database migrations

The development Compose profile runs migrations before starting the API. Migration files are ordered SQL and are applied by the `migrate` service with `psql`; production deployment can use the same files from its release job.

The foundation migration contains only migration bookkeeping and application metadata. No broker, order, command, or connector tables exist in this slice.
