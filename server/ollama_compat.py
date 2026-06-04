"""
Ollama-compatible API endpoints (/api/chat, /api/embed).
Allows apps using the `ollama` Python library to route through this proxy
without any code changes — just point OLLAMA_HOST at the proxy.

Routing logic mirrors the OpenAI-compat endpoint:
  - /api/chat  → same hold/reject/send/offline logic, streaming supported
  - /api/embed → always forwarded to Ollama (Claude has no embeddings API)
"""
import json
import time
from datetime import datetime

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import backends, db, utils

GANDALF = "gandalfthewhite"


# ── Format converters ─────────────────────────────────────────────────────────

def _ollama_to_openai(body: dict) -> dict:
    opts = body.get("options", {})
    return {
        "model": body.get("model", "llama3"),
        "messages": body.get("messages", []),
        "stream": False,
        "max_tokens": opts.get("num_predict", 4096),
    }


def _openai_to_ollama(openai_resp: dict, model: str) -> dict:
    choice = openai_resp.get("choices", [{}])[0]
    usage = openai_resp.get("usage", {})
    return {
        "model": model,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "message": choice.get("message", {"role": "assistant", "content": ""}),
        "done": True,
        "done_reason": choice.get("finish_reason", "stop"),
        "eval_count": usage.get("completion_tokens", 0),
        "prompt_eval_count": usage.get("prompt_tokens", 0),
    }


# ── Streaming helpers ─────────────────────────────────────────────────────────

async def _stream_ollama(body: dict):
    """Pass a streaming /api/chat request straight through to Ollama."""
    payload = {**body, "stream": True}
    async with httpx.AsyncClient(timeout=300.0) as client:
        async with client.stream("POST", f"{backends.OLLAMA_URL}/api/chat", json=payload) as resp:
            async for line in resp.aiter_lines():
                if line:
                    yield line + "\n"


async def _stream_claude_as_ollama(body: dict):
    """Call Claude with streaming and yield Ollama-format NDJSON chunks."""
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic()

    messages = body.get("messages", [])
    model = body.get("model", "unknown")
    system_content = None
    filtered = []
    for msg in messages:
        if msg["role"] == "system":
            system_content = msg["content"]
        else:
            filtered.append({"role": msg["role"], "content": msg["content"]})

    kwargs = {
        "model": backends.CLAUDE_MODEL,
        "max_tokens": body.get("options", {}).get("num_predict", 4096),
        "messages": filtered,
    }
    if system_content:
        kwargs["system"] = system_content

    async with client.messages.stream(**kwargs) as stream:
        async for text in stream.text_stream:
            yield json.dumps({
                "model": model,
                "created_at": datetime.utcnow().isoformat() + "Z",
                "message": {"role": "assistant", "content": text},
                "done": False,
            }) + "\n"

        final = await stream.get_final_message()
        yield json.dumps({
            "model": model,
            "created_at": datetime.utcnow().isoformat() + "Z",
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "done_reason": final.stop_reason or "stop",
            "eval_count": final.usage.output_tokens,
            "prompt_eval_count": final.usage.input_tokens,
        }) + "\n"


# ── Route handlers ────────────────────────────────────────────────────────────

async def handle_ollama_chat(request: Request):
    body = await request.json()
    is_streaming = body.get("stream", True)  # Ollama default is stream=True
    model = body.get("model", "unknown")

    flag = await db.get_flag()
    online = await utils.is_host_online(GANDALF)

    # Streaming requests can't be queued — fall back to Claude if not hitting GPU
    if is_streaming:
        if flag == "send" and online:
            return StreamingResponse(_stream_ollama(body), media_type="application/x-ndjson")
        else:
            return StreamingResponse(
                _stream_claude_as_ollama(body), media_type="application/x-ndjson"
            )

    # Non-streaming: same routing logic as /v1/chat/completions ───────────────
    if not online:
        oai_resp, duration_ms = await backends.call_claude(_ollama_to_openai(body))
        backend, model_used = "claude", oai_resp.get("model", backends.CLAUDE_MODEL)

    elif flag == "reject":
        oai_resp, duration_ms = await backends.call_claude(_ollama_to_openai(body))
        backend, model_used = "claude", oai_resp.get("model", backends.CLAUDE_MODEL)

    elif flag == "hold":
        oai_resp, duration_ms = await backends.call_claude(_ollama_to_openai(body))
        backend, model_used = "claude", oai_resp.get("model", backends.CLAUDE_MODEL)

    else:  # send + online → native Ollama
        try:
            start = time.monotonic()
            async with httpx.AsyncClient(timeout=300.0) as client:
                r = await client.post(
                    f"{backends.OLLAMA_URL}/api/chat", json={**body, "stream": False}
                )
                r.raise_for_status()
            duration_ms = int((time.monotonic() - start) * 1000)
            ollama_resp = r.json()
            model_used = ollama_resp.get("model", model)
            await db.record_direct(
                request_json=json.dumps(body),
                response_json=json.dumps(ollama_resp),
                backend="gpu",
                model_requested=model,
                model_used=model_used,
                duration_ms=duration_ms,
            )
            return JSONResponse(ollama_resp)
        except Exception as e:
            oai_resp, duration_ms = await backends.call_claude(_ollama_to_openai(body))
            backend, model_used = "claude", oai_resp.get("model", backends.CLAUDE_MODEL)

    ollama_resp = _openai_to_ollama(oai_resp, model)
    await db.record_direct(
        request_json=json.dumps(body),
        response_json=json.dumps(ollama_resp),
        backend=backend,
        model_requested=model,
        model_used=model_used,
        duration_ms=duration_ms,
    )
    return JSONResponse(ollama_resp)


async def handle_ollama_embed(request: Request):
    """
    Embeddings are always forwarded to Ollama — Claude has no embeddings API.
    Returns 503 if gandalfthewhite is offline.
    """
    body = await request.json()
    online = await utils.is_host_online(GANDALF)

    if not online:
        return JSONResponse(
            {"error": f"{GANDALF} is offline — embeddings unavailable. "
                      "Set flag to 'reject' won't help; Claude has no embeddings API."},
            status_code=503,
        )

    path = request.url.path  # preserve /api/embed vs /api/embeddings
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{backends.OLLAMA_URL}{path}", json=body)
        r.raise_for_status()

    return JSONResponse(r.json())
