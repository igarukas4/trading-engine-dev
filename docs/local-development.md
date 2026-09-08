# Local development

Start the executable foundation, PostgreSQL migration, FastAPI status API, and Indonesian System surface together:

```bash
docker compose -f compose.dev.yml up --build
```

Open `http://localhost:3000` for the read-only System page or `http://localhost:8000/docs` for the API contract. The local profile uses development-only credentials. Production secrets remain file-based and are never sent to the browser.

This slice intentionally has no broker connector or trading command route. The System response reports those capabilities as unavailable/disabled until a later issue adds them.
