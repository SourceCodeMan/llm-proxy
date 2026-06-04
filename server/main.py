import asyncio
import json
import logging
from contextlib import asynccontextmanager

from typing import Optional
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from . import backends, db, utils, worker
from .ollama_compat import handle_ollama_chat, handle_ollama_embed
from .whisper_compat import handle_transcription

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

GANDALF = "gandalfthewhite"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    task = asyncio.create_task(worker.queue_worker())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="LLM Proxy", version="1.0.0", lifespan=lifespan)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    callback_url = body.pop("callback_url", None)
    model_requested = body.get("model", "unknown")

    flag = await db.get_flag()
    online = await utils.is_host_online(GANDALF)

    # --- Routing logic ---
    if not online:
        return await _route_to_claude(body, model_requested, "gandalfthewhite offline")

    if flag == "reject":
        return await _route_to_claude(body, model_requested, "reject flag set")

    if flag == "hold":
        return await _route_to_claude(body, model_requested, "hold flag (gaming mode)")

    # flag == "send" and online → try GPU, fall back to Claude on error
    try:
        response, duration_ms = await backends.call_ollama(body)
        backend = "gpu"
        model_used = response.get("model", model_requested)
    except Exception as e:
        logger.warning(f"Ollama failed ({e}), falling back to Claude")
        return await _route_to_claude(body, model_requested, f"Ollama error: {e}")

    await db.record_direct(
        request_json=json.dumps(body),
        response_json=json.dumps(response),
        backend=backend,
        model_requested=model_requested,
        model_used=model_used,
        duration_ms=duration_ms,
    )
    return JSONResponse(response)


async def _route_to_claude(body: dict, model_requested: str, reason: str):
    logger.info(f"Routing to Claude ({reason})")
    response, duration_ms = await backends.call_claude(body)
    model_used = response.get("model", backends.CLAUDE_MODEL)
    await db.record_direct(
        request_json=json.dumps(body),
        response_json=json.dumps(response),
        backend="claude",
        model_requested=model_requested,
        model_used=model_used,
        duration_ms=duration_ms,
    )
    return JSONResponse(response)


# --- Ollama-compatible endpoints (for apps using the ollama Python library) ---

@app.post("/api/chat")
async def ollama_chat(request: Request):
    return await handle_ollama_chat(request)


@app.post("/api/embed")
@app.post("/api/embeddings")
async def ollama_embed(request: Request):
    return await handle_ollama_embed(request)


@app.post("/v1/audio/transcriptions")
async def audio_transcriptions(
    file: UploadFile,
    model: str = Form("whisper-1"),
    language: Optional[str] = Form(None),
    prompt: Optional[str] = Form(None),
    response_format: str = Form("json"),
    temperature: Optional[float] = Form(None),
):
    return await handle_transcription(file, model, language, prompt, response_format, temperature)


@app.get("/api/tags")
async def ollama_tags():
    """Ollama /api/tags compatibility — n8n uses this to test the connection."""
    online = await utils.is_host_online(GANDALF)
    if online:
        try:
            async with __import__("httpx").AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{backends.OLLAMA_URL}/api/tags")
                return JSONResponse(r.json())
        except Exception:
            pass
    # Return a minimal valid response so n8n accepts the credential
    return JSONResponse({"models": []})


# --- Flag management ---

@app.get("/flag")
async def get_flag():
    flag = await db.get_flag()
    online = await utils.is_host_online(GANDALF)
    depth = await db.get_queue_depth()
    return {"flag": flag, "gandalf_online": online, "queue_depth": depth}


@app.post("/flag/{value}")
async def set_flag(value: str):
    if value not in ("hold", "reject", "send"):
        raise HTTPException(status_code=400, detail="Flag must be: hold, reject, or send")
    await db.set_flag(value)
    logger.info(f"Flag set to: {value}")
    return {"flag": value}


# --- Job status & history ---

@app.get("/jobs/{job_id}")
async def get_job(job_id: int):
    job = await db.get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return dict(job)


@app.get("/history")
async def history(limit: int = 50):
    rows = await db.get_history(limit)
    return [dict(r) for r in rows]


# --- Status overview ---

@app.get("/status")
async def status():
    flag = await db.get_flag()
    online = await utils.is_host_online(GANDALF)
    depth = await db.get_queue_depth()

    if not online:
        routing = "gandalfthewhite offline → Claude"
    elif flag == "reject":
        routing = "reject flag → Claude"
    elif flag == "hold":
        routing = f"hold flag → queuing ({depth} pending)"
    else:
        routing = "send flag + online → GPU"

    return {
        "flag": flag,
        "gandalf_online": online,
        "queue_depth": depth,
        "routing": routing,
    }
