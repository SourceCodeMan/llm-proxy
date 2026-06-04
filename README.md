# LLM Proxy

A self-hosted routing proxy that sits in front of your local GPU (Ollama) and automatically falls back to cloud AI services when your gaming PC is unavailable or in use. All your automation tools point at one URL — the proxy handles the rest.

## Architecture

```
n8n / bible-rag / any tool
          │
          ▼
   ubuntu-server:8742  (this proxy)
          │
          ├─── gandalfthewhite online + flag=send ──► Ollama (local GPU)
          │
          ├─── flag=hold (gaming mode) ──────────────► Claude Haiku / OpenAI Whisper
          │
          ├─── flag=reject ───────────────────────────► Claude Haiku / OpenAI Whisper
          │
          └─── gandalfthewhite offline ───────────────► Claude Haiku / OpenAI Whisper
```

## Routing Logic

The proxy checks two things on every request: **is gandalfthewhite reachable?** and **what is the current flag?**

### LLM Chat (`/v1/chat/completions`, `/api/chat`)

| Condition | Destination | Notes |
|---|---|---|
| `send` flag + gandalfthewhite online | **Local GPU (Ollama)** | Full speed, no cost |
| `send` flag + Ollama errors | **Claude Haiku** | Automatic fallback |
| `hold` flag (gaming mode) | **Claude Haiku** | Synchronous, workflows unaffected |
| `reject` flag | **Claude Haiku** | Force cloud permanently |
| gandalfthewhite offline | **Claude Haiku** | PC is off, auto cloud |

### Audio Transcription (`/v1/audio/transcriptions`)

| Condition | Destination | Notes |
|---|---|---|
| `send` flag + gandalfthewhite online | **Local Whisper** (`gandalfthewhite:8001`) | Full speed, no cost |
| `hold` / `reject` / offline | **OpenAI Whisper API** | `whisper-1` model |

### Embeddings (`/api/embed`, `/api/embeddings`)

| Condition | Destination | Notes |
|---|---|---|
| gandalfthewhite online | **Ollama** (nomic-embed-text etc.) | Always routed locally |
| gandalfthewhite offline | **503 error** | Claude has no embeddings API |

### Flag Reference

| Flag | Tray colour | Behaviour |
|---|---|---|
| `send` | 🟢 Green | Route to GPU. Fall back to Claude on error. |
| `hold` | 🟡 Amber | Gaming mode — all jobs go to cloud immediately. |
| `reject` | 🔴 Red | Force cloud permanently regardless of GPU state. |

Flag is set via the Windows tray app or directly:
```bash
curl -X POST http://ubuntu-server:8742/flag/hold
curl -X POST http://ubuntu-server:8742/flag/send
curl -X POST http://ubuntu-server:8742/flag/reject
```

---

## Components

### `server/` — FastAPI proxy (runs on ubuntu-server)

| File | Purpose |
|---|---|
| `main.py` | App entrypoint, route definitions, OpenAI-compat chat endpoint |
| `backends.py` | Ollama and Claude API callers |
| `ollama_compat.py` | Ollama-native `/api/chat`, `/api/embed`, `/api/tags` endpoints |
| `whisper_compat.py` | Audio transcription proxy with OpenAI fallback |
| `worker.py` | Background queue drain loop |
| `db.py` | SQLite: job history, flag state |
| `utils.py` | Ping check for gandalfthewhite |

### `tray/` — Windows system tray app (runs on gandalfthewhite)

Right-click tray icon to set the routing flag. Icon colour reflects current state. Auto-refreshes every 30 seconds. Server URL configured via right-click → Configure server.

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/chat/completions` | OpenAI-compatible chat (used by n8n, most tools) |
| `POST` | `/api/chat` | Ollama-native chat (used by apps with `ollama` Python library) |
| `POST` | `/api/embed` | Ollama-native embeddings |
| `POST` | `/api/embeddings` | Ollama-native embeddings (legacy path) |
| `GET` | `/api/tags` | Ollama model list (used by n8n connection test) |
| `POST` | `/v1/audio/transcriptions` | Whisper-compatible transcription |
| `GET` | `/flag` | Get current flag + status |
| `POST` | `/flag/{value}` | Set flag (`send`, `hold`, `reject`) |
| `GET` | `/status` | Flag, GPU online status, queue depth, routing description |
| `GET` | `/history` | Job history with backend, model, and duration |
| `GET` | `/jobs/{id}` | Look up a specific job by ID |

---

## Setup

### Server (Ubuntu)

```bash
# Copy server/ folder, .env.example, llm-proxy.service to the server
sudo cp -r llm-proxy/ /opt/llm-proxy
cd /opt/llm-proxy

python3 -m venv venv
venv/bin/pip install -r server/requirements.txt

cp .env.example .env
nano .env   # fill in your keys

sudo cp llm-proxy.service /etc/systemd/system/
sudo nano /etc/systemd/system/llm-proxy.service  # set User=
sudo systemctl daemon-reload
sudo systemctl enable --now llm-proxy

# Add gandalfthewhite to /etc/hosts if not resolvable via DNS
echo "192.168.x.x  gandalfthewhite" | sudo tee -a /etc/hosts
```

### Environment Variables (`.env`)

```env
ANTHROPIC_API_KEY=sk-ant-...       # Claude fallback
OPENAI_API_KEY=sk-proj-...         # OpenAI Whisper fallback
OLLAMA_URL=http://gandalfthewhite:11434
WHISPER_URL=http://gandalfthewhite:8001
CLAUDE_MODEL=claude-haiku-4-5
```

### Tray App (Windows — gandalfthewhite)

```powershell
cd tray
pip install -r requirements.txt
python tray_app.py
```

Right-click → **Configure server...** → enter `http://ubuntu-server:8742`

A startup task is registered in Windows Task Scheduler under `\Users\tomch\LLM Proxy Tray` to launch automatically on login.

---

## Pointing Tools at the Proxy

**n8n:** Update the Ollama credential Base URL to `http://host.docker.internal:8742`

**Apps using the `ollama` Python library:** Set env var `OLLAMA_HOST=http://host.docker.internal:8742`

**Apps using OpenAI SDK:**
```python
from openai import OpenAI
client = OpenAI(base_url="http://ubuntu-server:8742/v1", api_key="none")
```

**Whisper callers:** Change `http://gandalfthewhite:8001` → `http://ubuntu-server:8742`

---

## Monitoring

```bash
# Current routing state
curl http://ubuntu-server:8742/status

# Job history (GPU vs Claude, timing comparison)
curl http://ubuntu-server:8742/history | python3 -m json.tool

# Live service logs
sudo journalctl -u llm-proxy -f
```

---

## Connected Services

| Service | URL | Routes through proxy |
|---|---|---|
| n8n | `ubuntu-server:5678` | ✅ Yes (Ollama credential) |
| bible-rag | `ubuntu-server:8000` | ✅ Yes (`OLLAMA_HOST`) |
| medicare-rag | `ubuntu-server:8001` | ❌ No (embeddings only, OpenAI) |
| Whisper | `gandalfthewhite:8001` | — (is the upstream target) |
| Ollama | `gandalfthewhite:11434` | — (is the upstream target) |
| DNS Dashboard | `ubuntu-server:5380` | — (unrelated) |
