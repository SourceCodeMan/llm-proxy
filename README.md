# llm-proxy

A self-hosted FastAPI proxy that sits between [n8n](https://n8n.io/) (or any HTTP client) and local/cloud LLM backends. Routes requests to a local GPU (Ollama on `gandalfthewhite`) or the Claude/OpenAI APIs based on a live flag — no workflow changes needed when switching modes.

## Architecture

```
n8n (Docker)
    │
    └── http://host.docker.internal:8742
              │
         llm-proxy (Docker, port 8742)
              │
    ┌─────────┴──────────┐
    │  flag = "send"     │  flag = "reject" / "hold" / offline
    ▼                    ▼
Ollama (gandalfthewhite:11434)    Claude API / OpenAI API
Whisper (gandalfthewhite:8001)    OpenAI Whisper API
```

## Routing flags

| Flag | LLM | Whisper |
|------|-----|---------|
| `send` + gandalfthewhite online | Local GPU (Ollama) | Local GPU (Whisper) |
| `reject` | Claude API | OpenAI Whisper API |
| `hold` | Claude API | OpenAI Whisper API |
| gandalfthewhite offline (any flag) | Claude API | OpenAI Whisper API |

## API endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /flag` | Current flag + gandalf status |
| `POST /flag/{value}` | Set flag (`send` / `reject` / `hold`) |
| `GET /status` | Full status including queue depth and routing |
| `POST /api/generate` | Ollama-compatible generate |
| `POST /api/chat` | Ollama-compatible chat |
| `POST /api/embed` | Ollama-compatible embeddings |
| `POST /v1/chat/completions` | OpenAI-compatible chat completions |
| `POST /v1/audio/transcriptions` | OpenAI-compatible Whisper transcription |
| `GET /history` | Recent request history |
| `GET /jobs/{job_id}` | Job status |

## Deployment (Docker)

### Prerequisites
- Docker (snap or native)
- `.env` file with API keys (see `.env.example`)

### Start

```bash
cd ~/llm-proxy
docker compose up -d
```

### Stop

```bash
docker compose down
```

### View logs

```bash
docker logs -f llm-proxy
```

### Rebuild after code changes

```bash
docker compose up -d --build
```

## Configuration

Copy `.env.example` to `.env` and fill in your keys:

```env
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
OLLAMA_URL=http://gandalfthewhite:11434
WHISPER_URL=http://gandalfthewhite:8001
CLAUDE_MODEL=claude-haiku-4-5
```

## Project structure

```
llm-proxy/
├── compose.yml           # Docker Compose config
├── Dockerfile
├── .env                  # API keys (not committed)
├── .env.example
└── server/
    ├── main.py           # FastAPI app, all endpoints
    ├── backends.py       # call_ollama() / call_claude()
    ├── ollama_compat.py  # /api/generate + /api/chat translation layer
    ├── whisper_compat.py # /v1/audio/transcriptions routing
    ├── worker.py         # Background queue worker
    ├── db.py             # SQLite flag + job history
    ├── utils.py          # is_host_online()
    └── requirements.txt
```

## n8n integration

The n8n container reaches the proxy via `host.docker.internal:8742`. The `docker-compose.yml` for n8n must include:

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

Workflows call `http://host.docker.internal:8742/api/generate` (Ollama-style) or `http://host.docker.internal:8742/v1/chat/completions` (OpenAI-style) — the proxy handles routing transparently.

## Changing the flag

From anywhere on the network:

```bash
# Switch to GPU
curl -X POST http://ubuntu-server.home.lan:8742/flag/send

# Switch to Claude (e.g. when gaming / GPU busy)
curl -X POST http://ubuntu-server.home.lan:8742/flag/reject
```
