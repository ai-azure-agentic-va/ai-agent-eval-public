# Evaluations Framework

An evaluation platform for LangGraph agents. A Streamlit UI sits in front of a
FastAPI eval service that runs golden datasets against an agent and scores every
answer with Azure OpenAI LLM-as-judge metrics. Results are stored in LangSmith
(default) or Azure Cosmos DB. Users sign in with Entra ID, and each agent call
forwards the signed-in user's bearer token.

## Features

- **Golden datasets.** Upload `.xlsx`, `.xls` or `.csv` files with `question` and
  `reference_answer` columns (`expected_agent`, `tags` and `name` are optional).
  Rows are validated before saving. Re-uploading a dataset under the same name
  creates a new version.
- **Agent runs.** There are three execution modes: sequential with independent
  questions (the default), sequential with one shared conversation (multi-turn),
  or parallel.
- **LLM-as-judge scoring.** Nine metrics, each scored 0–1. Each metric has its own
  threshold (default 0.80), and a row's `tags` decide which judges run on it.
- **Results.** Watch runs as they progress and read the judge's reasoning for each
  case. Export results to CSV or PDF, resume failed runs, or retry failed judges
  without calling the agent again.
- **Tracing (optional).** Send the pipeline's own traces (agent call and judge
  spans) to LangSmith.

## Getting started

Copy the template and fill in your values (see [Configuration](#configuration)):

```bash
cp .env.example .env
```

### Docker (single container, one port)

```bash
docker build -t eval-streamlit-framework .
docker run -d --add-host=host.docker.internal:host-gateway --env-file .env \
  -p 8501:8501 eval-streamlit-framework
curl -s http://localhost:8501/api/health
```

Open http://localhost:8501. Inside the container, nginx serves the UI and routes
`/auth/*` to the service unchanged. It also routes `/api/*` to the service, with
the prefix stripped.

If the agent runs on `127.0.0.1`/`localhost` on your machine, the service reaches
it through `host.docker.internal` (override with `AGENT_LOOPBACK_HOST`). Docker
Desktop provides that name; on Linux the `--add-host` flag above maps it, and the
agent must listen on an address the container can reach (for example `0.0.0.0`).

To keep the service log on the host, run `mkdir -p logs` first and add
`-v "$PWD/logs:/app/logs"` to the run command. The container runs as UID 1000, so
on Linux that directory must be writable by UID 1000.

### Native (Python 3.12, two terminals)

The Docker image uses Python 3.12; use the same version for a native run.

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python -m uvicorn service.main:app --port 8100   # terminal 1: eval service
python -m streamlit run ui/app.py                # terminal 2: UI on :8501
```

The service and the UI both load `.env` from the repository root. Variables
already set in the shell take precedence. The UI finds the service through
`EVAL_API_URL` (default `http://127.0.0.1:8100`). Browse the UI at
`http://localhost:8501`, not `127.0.0.1`: the sign-in callback always uses
`localhost` and cookies are scoped per hostname, so a sign-in started from
`127.0.0.1` fails with "state mismatch or expired".

### Sign-in

Register these redirect URIs on your Entra app registration:
`http://localhost:8501/auth/callback` for Docker and
`http://localhost:8100/auth/callback` for native. For local work without SSO, set
`DISABLE_AUTH=1`. The sidebar then shows an optional bearer-token field instead of
the login card.

## Configuration

Every environment-specific setting comes from environment variables, and
`.env.example` lists them all. No real endpoints or credentials are committed.

| Area | Variables |
|---|---|
| Judge model | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT_NAME` (default `gpt-5.1`), `API_VERSION`, `AZURE_OPENAI_USE_MANAGED_IDENTITY` (default `true`: managed identity or the Azure credential chain, such as `az login`), `AZURE_CLIENT_ID`, `AZURE_OPEN_AI_KEY` (used only when managed identity is `false`) |
| Agent under test | `LANGGRAPH_BASE_URL` (default `http://127.0.0.1:2026`), `LANGGRAPH_ASSISTANT_ID` (default `chat`), `LANGGRAPH_TIMEOUT_SECONDS` (default 300), `LANGGRAPH_BEARER_TOKEN` (optional static token), `EVAL_APPLICATIONS` (optional JSON-list registry override) |
| Storage | `EVAL_STORAGE` = `langsmith` (default) or `cosmos`. LangSmith needs `LANGSMITH_EVALS_KEY` (falls back to `LANGSMITH_API_KEY`). Cosmos DB needs `AZURE_COSMOS_ENDPOINT` and `AZURE_COSMOS_DATABASE` (default `eval-framework`), uses Azure AD credentials, and expects existing `datasets` (partition key `/dataset_id`), `runs` (`/run_id`) and `rows` (`/run_id`) containers |
| Tracing | `LANGSMITH_TRACING` (`true` to enable), `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT` (default `eval-framework`) |
| Sign-in | `ENTRA_TENANT_ID`, `ENTRA_CLIENT_ID`, `ENTRA_CLIENT_SECRET`, `AUTH_SECRET` (shared by the service and the UI), `API_SCOPE`, `STREAMLIT_URL`, `AUTH_REDIRECT_URI`, `DISABLE_AUTH`, `AUTH_REQUIRE_API_SESSION` |
| UI | `EVAL_API_URL` (the Docker entrypoint sets it inside the container) |
| Tuning | `EVAL_ALLOWED_METRICS`, `EVAL_RUN_CONCURRENCY` (default 4), `EVAL_JUDGE_CONCURRENCY`, `EVAL_JUDGE_MIN_INTERVAL_MS`, `EVAL_JUDGE_MAX_ATTEMPTS`, `EVAL_JUDGE_RETRY_BUDGET_S`, `EVAL_JUDGE_TIMEOUT_S`, `EVAL_JUDGE_MAX_OUTPUT_TOKENS`, `EVAL_JUDGE_MAX_PROMPT_BYTES` |
| Logging | `LOG_LEVEL` (default `INFO`), `LOG_FORMAT` (`text` or `json`), `LOG_FILE` (optional rotating file) |

## Project layout

```
service/            FastAPI eval service: routes, run engine, agent client, auth
service/judges/     one LLM-as-judge module per metric, plus the judge panel
service/storage/    LangSmith and Cosmos DB storage backends
prompts/            judge prompt templates (see prompts/README.md)
ui/                 Streamlit UI: Datasets, Run and Results pages
docker/             container entrypoint and nginx front door
.streamlit/         Streamlit theme
Dockerfile          single image: service + UI + nginx on port 8501
requirements.txt    runtime dependencies
pyproject.toml      lint (ruff) settings
.env.example        configuration template (placeholders only)
```

## Metrics

Every metric is scored 0–1, and higher is better. Rows tagged jailbreak, security,
red team or adversarial are **security** rows. Rows tagged fallback, refusal or
honesty are **honesty** rows.

| Key | Measures |
|---|---|
| `correctness` | how well the answer matches the reference answer (shown as "accuracy") |
| `relevance` | whether the response addresses the question asked |
| `groundedness` | whether the answer's claims are supported by the retrieved context (shown as "faithfulness") |
| `bias` | absence of unjustified social or demographic bias |
| `retrieval` | whether the retrieved context contains what's needed to answer |
| `context_precision` | how much of the retrieved context is relevant |
| `context_recall` | whether the retrieved context covers the reference answer's claims |
| `fallback` | honest "not found" or refusal instead of made-up content (honesty rows only) |
| `jailbreak` | resistance to prompt-injection and jailbreak attempts (security rows only) |

Security rows are scored only on `relevance`, `jailbreak` and `bias`. All other
rows get every metric except `jailbreak` and `fallback`, and honesty rows also get
`fallback`. To limit the metrics offered, set `EVAL_ALLOWED_METRICS` to a
comma-separated list of keys; leave it unset or blank to allow all nine. If the
list contains an unknown key, new runs are rejected with HTTP 503 and `/health`
reports it. A case passes only when every selected metric that applies to it meets
its threshold. Each case also records agent latency, token usage and whether
routing matched `expected_agent`.

## Security notes

- **Never commit `.env`.** `.gitignore` and `.dockerignore` exclude it, so pass it
  at runtime with `--env-file`. Use a long random `AUTH_SECRET` (`.env.example`
  shows how to generate one).
- **Keep `AUTH_REQUIRE_API_SESSION=true`** (the template sets it). When it is
  unset, `/api/*` accepts requests without a session. When it is on, nginx
  requires a valid, unexpired `eval_session` cookie on every `/api/*` request
  except `/api/health`. The service decodes the forwarded bearer token but does
  not validate it; the agent under test does that.
- **This check exists only in the Docker image.** In a native run, the service on
  port 8100 has no session check. Keep it on `127.0.0.1` (uvicorn's default host).
- **`DISABLE_AUTH` turns off the UI's sign-in, which opens the whole app.** The UI
  calls the service directly (in Docker it does not go through nginx's `/api/*`
  check), so anyone who can open the UI can upload datasets, start runs and read
  results, even with `AUTH_REQUIRE_API_SESSION=true`. Use it for local work only.
- **Behind HTTPS,** set `STREAMLIT_URL` to your `https://` origin, which makes the
  cookies `Secure`. Also set `AUTH_REDIRECT_URI` to the callback registered for
  `https://<your-host>/auth/callback`.

## Releases

Public releases are cut as `release/X.Y.Z` branches. Each release is a
versioned snapshot of the source.

## License

No license file is included, so no license is granted. All rights reserved by
the project maintainers.
