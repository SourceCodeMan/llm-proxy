import os
import time

import httpx
from anthropic import AsyncAnthropic

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://gandalfthewhite:11434")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

_anthropic = AsyncAnthropic()  # reads ANTHROPIC_API_KEY from env


async def call_ollama(request: dict) -> tuple[dict, int]:
    payload = {**request, "stream": False}
    start = time.monotonic()
    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.post(
            f"{OLLAMA_URL}/v1/chat/completions",
            json=payload,
        )
        resp.raise_for_status()
    duration_ms = int((time.monotonic() - start) * 1000)
    return resp.json(), duration_ms


async def call_claude(request: dict) -> tuple[dict, int]:
    messages = request.get("messages", [])
    system_content = None
    filtered = []
    for msg in messages:
        if msg["role"] == "system":
            system_content = msg["content"]
        else:
            filtered.append({"role": msg["role"], "content": msg["content"]})

    kwargs = {
        "model": CLAUDE_MODEL,
        "max_tokens": request.get("max_tokens", 4096),
        "messages": filtered,
    }
    if system_content:
        kwargs["system"] = system_content

    start = time.monotonic()
    response = await _anthropic.messages.create(**kwargs)
    duration_ms = int((time.monotonic() - start) * 1000)

    # Return OpenAI-compatible format so callers need no changes
    return {
        "id": response.id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": response.model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": response.content[0].text,
                },
                "finish_reason": response.stop_reason or "stop",
            }
        ],
        "usage": {
            "prompt_tokens": response.usage.input_tokens,
            "completion_tokens": response.usage.output_tokens,
            "total_tokens": response.usage.input_tokens + response.usage.output_tokens,
        },
    }, duration_ms
