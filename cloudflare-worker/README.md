# Dashboards Worker

Cloudflare Worker that fronts the `DASHBOARDS_BUCKET` R2 bucket and serves
each dashboard's JSON to its frontend. Replaces the per-dashboard "commit
JSON to GitHub" flow.

## Data flow

```
Lambda (refresh job)  ──put_object──►  R2 bucket  ──get──►  Worker  ──fetch──►  Dashboard HTML
```

## Lambdas → R2 keys

| Lambda                                       | Bucket key                                       |
| -------------------------------------------- | ------------------------------------------------ |
| `campaigns_life` (games dashboard)           | `game-campaigns/campaigns_life.json`             |
| `superage_metrics_lambda_updated.py`         | `superage-dashboard/superage-metrics.json`       |
| `superage_comparison_lambda.py`              | `superage-dashboard/superage-comparison.json`    |

Each Lambda reads its R2 credentials from a single Secrets Manager secret
referenced by `R2_SECRET_ARN` (keys: `account_id`, `access_key_id`,
`secret_access_key`, `bucket_name`).

## Worker URL shape

```
GET https://dashboard.pardon-ventures-06b.workers.dev/                                          → legacy: game-campaigns/campaigns_life.json
GET https://dashboard.pardon-ventures-06b.workers.dev/superage-dashboard/superage-metrics.json
GET https://dashboard.pardon-ventures-06b.workers.dev/superage-dashboard/superage-comparison.json
```

The URL path **is** the R2 key. Any path not in the Worker's `ALLOWED_KEYS`
allowlist returns 404, so the bucket isn't browsable.

## Adding a new dashboard

1. Point the new Lambda at R2 via `R2_SECRET_ARN` and set `R2_FILE_PATH=<dashboard>/<file>.json`.
2. Add the key to `ALLOWED_KEYS` in `worker.js`.
3. Add the dashboard's browser origin to `ALLOWED_ORIGINS` (or it gets 403 in the browser).
4. Re-deploy the Worker (`wrangler deploy`).

## CORS

`ALLOWED_ORIGINS` is the allowlist. Add the dashboard's public origin
before the page can fetch from the Worker — otherwise the browser will
fail the request with a 403.

Server-to-server requests (no `Origin` header) bypass the allowlist by
design — convenient for `curl` health checks and Lambda → Worker probes.
