"""
Ollama-compatible API endpoints (/api/chat, /api/generate, /api/embed).
Allows apps using the `ollama` Python library to route through this proxy
without any code changes — just point OLLAMA_HOST at the proxy.

Routing logic mirrors the OpenAI-compat endpoint:
  - /api/chat     → same hold/reject/send/offline logic, streaming supported
  - /api/generate → same routing; converts prompt↔response format automatically
  - /api/embed    → always forwarded to Ollama (Claude has no embeddings API)
"""
import json
import logging
import time
from datetime import datetime

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import backends, db, utils

GANDALF = "gandalfthewhite"
logger = logging.getLogger(__name__)


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


def _generate_to_openai(body: dict) -> dict:
    """Convert /api/generate format (prompt string) to OpenAI chat format."""
    opts = body.get("options", {})
    return {
        "model": body.get("model", "llama3"),
        "messages": [{"role": "user", "content": body.get("prompt", "")}],
        "stream": False,
        "max_tokens": opts.get("num_predict", 4096),
    }


def _openai_to_generate(openai_resp: dict, model: str) -> dict:
    """Convert OpenAI chat response to /api/generate format."""
    choice = openai_resp.get("choices", [{}])[0]
    usage = openai_resp.get("usage", {})
    content = choice.get("message", {}).get("content", "")
    return {
        "model": model,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "response": content,
        "done": True,
        "done_reason": choice.get("finish_reason", "stop"),
        "eval_count": usage.get("completion_tokens", 0),
        "prompt_eval_count": usage.get("prompt_tokens", 0),
    }


async def _record(*, request_json, response_json, backend, model_requested, model_used, duration_ms):
    """Record a completed request; log and swallow DB errors so they never affect routing."""
    try:
        await db.record_direct(
            request_json=request_json,
            response_json=response_json,
            backend=backend,
            model_requested=model_requested,
            model_used=model_used,
            duration_ms=duration_ms,
        )
    except Exception as e:
        logger.error(f"DB record failed (non-fatal): {e}")


# ── Streaming helpers ─────────────────────────────────────────────────────────

async def _stream_ollama(body: dict):
    """Pass a streaming /api/chat request straight through to Ollama."""
    payload = {**body, "stream": True}
    async with httpx.AsyncClient(timeout=300.0) as client:
        async with client.stream("POST", f"{backends.OLLAMA_URL}/api/chat", json=payload) as resp:
            async for line in resp.aiter_lines():
                if line:
                    yield line + "\n"


async def _stream_ollama_generate(body: dict):
    """Pass a streaming /api/generate request straight through to Ollama."""
    payload = {**body, "stream": True}
    async with httpx.AsyncClient(timeout=300.0) as client:
        async with client.stream("POST", f"{backends.OLLAMA_URL}/api/generate", json=payload) as resp:
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


async def _stream_claude_as_generate(body: dict):
    """Call Claude with streaming and yield /api/generate-format NDJSON chunks."""
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic()

    model = body.get("model", "unknown")
    prompt = body.get("prompt", "")
    opts = body.get("options", {})

    kwargs = {
        "model": backends.CLAUDE_MODEL,
        "max_tokens": opts.get("num_predict", 4096),
        "messages": [{"role": "user", "content": prompt}],
    }

    async with client.messages.stream(**kwargs) as stream:
        async for text in stream.text_stream:
            yield json.dumps({
                "model": model,
                "created_at": datetime.utcnow().isoformat() + "Z",
                "response": text,
                "done": False,
            }) + "\n"

        final = await stream.get_final_message()
        yield json.dumps({
            "model": model,
            "created_at": datetime.utcnow().isoformat() + "Z",
            "response": "",
            "done": True,
            "done_reason": final.stop_reason or "stop",
            "eval_count": final.usage.output_tokens,
            "prompt_eval_count": final.usage.input_tokens,
        }) + "\n"


# ── Route handlers ────────────────────────────────────────────────────────────

def _wants_cloud(model: str) -> bool:
    """Per-request override: model 'claude'/'cloud' always routes to Claude,
    regardless of flag or GPU availability (e.g. meeting summaries)."""
    return model.lower().startswith(("claude", "cloud"))


async def handle_ollama_chat(request: Request):
    body = await request.json()
    is_streaming = body.get("stream", True)  # Ollama default is stream=True
    model = body.get("model", "unknown")

    flag = await db.get_flag()
    online = await utils.is_host_online(GANDALF)
    force_cloud = _wants_cloud(model)

    # Streaming requests can't be queued — fall back to Claude if not hitting GPU
    if is_streaming:
        if not force_cloud and flag == "send" and online:
            logger.info(f"[/api/chat] stream → GPU (flag={flag})")
            return StreamingResponse(_stream_ollama(body), media_type="application/x-ndjson")
        else:
            logger.info(f"[/api/chat] stream → Claude (flag={flag}, online={online})")
            return StreamingResponse(
                _stream_claude_as_ollama(body), media_type="application/x-ndjson"
            )

    # Non-streaming: GPU path (isolated try — DB errors never cause fallthrough)
    if not force_cloud and flag == "send" and online:
        logger.info(f"[/api/chat] → GPU (flag={flag})")
        ollama_resp = None
        ollama_err = None
        try:
            start = time.monotonic()
            async with httpx.AsyncClient(timeout=90.0) as client:
                r = await client.post(
                    f"{backends.OLLAMA_URL}/api/chat", json={**body, "stream": False}
                )
                r.raise_for_status()
            duration_ms = int((time.monotonic() - start) * 1000)
            ollama_resp = r.json()
        except Exception as e:
            ollama_err = e

        if ollama_resp is not None:
            model_used = ollama_resp.get("model", model)
            await _record(
                request_json=json.dumps(body),
                response_json=json.dumps(ollama_resp),
                backend="gpu",
                model_requested=model,
                model_used=model_used,
                duration_ms=duration_ms,
            )
            return JSONResponse(ollama_resp)
        else:
            logger.warning(f"[/api/chat] GPU failed ({ollama_err}), falling back to Claude")

    # Claude path (all non-GPU cases: cloud override, reject, hold, offline, or GPU failure)
    reason = (
        "cloud model requested" if force_cloud else
        "GPU error" if (flag == "send" and online) else
        f"flag={flag}" if flag != "send" else
        "offline"
    )
    logger.info(f"[/api/chat] → Claude ({reason})")
    oai_resp, duration_ms = await backends.call_claude(_ollama_to_openai(body))
    model_used = oai_resp.get("model", backends.CLAUDE_MODEL)
    ollama_resp = _openai_to_ollama(oai_resp, model)
    await _record(
        request_json=json.dumps(body),
        response_json=json.dumps(ollama_resp),
        backend="claude",
        model_requested=model,
        model_used=model_used,
        duration_ms=duration_ms,
    )
    return JSONResponse(ollama_resp)


async def handle_ollama_generate(request: Request):
    """
    Handle /api/generate requests (prompt string → response string format).
    Same hold/reject/send/offline routing as /api/chat.
    Forwards natively to Ollama when GPU is available; falls back to Claude otherwise.
    """
    body = await request.json()
    is_streaming = body.get("stream", False)  # generate default is stream=False
    model = body.get("model", "unknown")

    flag = await db.get_flag()
    online = await utils.is_host_online(GANDALF)
    force_cloud = _wants_cloud(model)

    # Streaming path ──────────────────────────────────────────────────────────
    if is_streaming:
        if not force_cloud and flag == "send" and online:
            logger.info(f"[/api/generate] stream → GPU (flag={flag})")
            return StreamingResponse(_stream_ollama_generate(body), media_type="application/x-ndjson")
        else:
            logger.info(f"[/api/generate] stream → Claude (flag={flag}, online={online})")
            return StreamingResponse(
                _stream_claude_as_generate(body), media_type="application/x-ndjson"
            )

    # Non-streaming: GPU path (isolated try — DB errors never cause fallthrough)
    if not force_cloud and flag == "send" and online:
        logger.info(f"[/api/generate] → GPU (flag={flag})")
        gen_resp = None
        ollama_err = None
        try:
            start = time.monotonic()
            async with httpx.AsyncClient(timeout=90.0) as client:
                r = await client.post(
                    f"{backends.OLLAMA_URL}/api/generate", json={**body, "stream": False}
                )
                r.raise_for_status()
            duration_ms = int((time.monotonic() - start) * 1000)
            gen_resp = r.json()
        except Exception as e:
            ollama_err = e

        if gen_resp is not None:
            model_used = gen_resp.get("model", model)
            await _record(
                request_json=json.dumps(body),
                response_json=json.dumps(gen_resp),
                backend="gpu",
                model_requested=model,
                model_used=model_used,
                duration_ms=duration_ms,
            )
            return JSONResponse(gen_resp)
        else:
            logger.warning(f"[/api/generate] GPU failed ({ollama_err}), falling back to Claude")

    # Claude path
    reason = (
        "cloud model requested" if force_cloud else
        "GPU error" if (flag == "send" and online) else
        f"flag={flag}" if flag != "send" else
        "offline"
    )
    logger.info(f"[/api/generate] → Claude ({reason})")
    oai_resp, duration_ms = await backends.call_claude(_generate_to_openai(body))
    model_used = oai_resp.get("model", backends.CLAUDE_MODEL)
    gen_resp = _openai_to_generate(oai_resp, model)
    await _record(
        request_json=json.dumps(body),
        response_json=json.dumps(gen_resp),
        backend="claude",
        model_requested=model,
        model_used=model_used,
        duration_ms=duration_ms,
    )
    return JSONResponse(gen_resp)


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
