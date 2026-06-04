"""
Whisper-compatible audio transcription proxy.

Routing:
  send + gandalfthewhite online  → local Whisper at gandalfthewhite:8001
  hold / reject / offline        → OpenAI Whisper API (whisper-1)

Accepts the same multipart form as the OpenAI /v1/audio/transcriptions endpoint:
  file            - audio file (required)
  model           - ignored locally, whisper-1 used for OpenAI fallback
  language        - optional ISO-639-1 code
  prompt          - optional context string
  response_format - json (default) | text | srt | vtt | verbose_json
  temperature     - optional float
"""
import os
from typing import Optional

import httpx
from fastapi import Form, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from . import db, utils

GANDALF = "gandalfthewhite"
WHISPER_URL = os.getenv("WHISPER_URL", "http://gandalfthewhite:8001")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_WHISPER_URL = "https://api.openai.com/v1/audio/transcriptions"


def _make_response(content: str | dict, response_format: str):
    if isinstance(content, dict):
        return JSONResponse(content)
    if response_format in ("text", "srt", "vtt"):
        return PlainTextResponse(content)
    # json / verbose_json returned as-is from upstream
    if isinstance(content, str):
        try:
            import json
            return JSONResponse(json.loads(content))
        except Exception:
            return PlainTextResponse(content)
    return JSONResponse(content)


async def handle_transcription(
    file: UploadFile,
    model: str = "whisper-1",
    language: Optional[str] = None,
    prompt: Optional[str] = None,
    response_format: str = "json",
    temperature: Optional[float] = None,
):
    flag = await db.get_flag()
    online = await utils.is_host_online(GANDALF)
    use_gpu = flag == "send" and online

    audio_bytes = await file.read()
    filename = file.filename or "audio.wav"
    content_type = file.content_type or "audio/wav"

    if use_gpu:
        try:
            return await _call_local_whisper(
                audio_bytes, filename, content_type,
                language, prompt, response_format, temperature
            )
        except Exception as e:
            # fall through to OpenAI
            pass

    return await _call_openai_whisper(
        audio_bytes, filename, content_type,
        language, prompt, response_format, temperature
    )


async def _call_local_whisper(
    audio_bytes, filename, content_type,
    language, prompt, response_format, temperature
):
    files = {"file": (filename, audio_bytes, content_type)}
    data = {"model": "whisper-1", "response_format": response_format}
    if language:   data["language"] = language
    if prompt:     data["prompt"] = prompt
    if temperature is not None: data["temperature"] = str(temperature)

    async with httpx.AsyncClient(timeout=300.0) as client:
        r = await client.post(
            f"{WHISPER_URL}/v1/audio/transcriptions",
            files=files,
            data=data,
        )
        r.raise_for_status()

    return _make_response(
        r.json() if response_format not in ("text", "srt", "vtt") else r.text,
        response_format
    )


async def _call_openai_whisper(
    audio_bytes, filename, content_type,
    language, prompt, response_format, temperature
):
    if not OPENAI_API_KEY:
        return JSONResponse(
            {"error": "No OPENAI_API_KEY set and local Whisper unavailable"},
            status_code=503
        )

    files = {"file": (filename, audio_bytes, content_type)}
    data = {"model": "whisper-1", "response_format": response_format}
    if language:   data["language"] = language
    if prompt:     data["prompt"] = prompt
    if temperature is not None: data["temperature"] = str(temperature)

    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(
            OPENAI_WHISPER_URL,
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            files=files,
            data=data,
        )
        r.raise_for_status()

    return _make_response(
        r.json() if response_format not in ("text", "srt", "vtt") else r.text,
        response_format
    )
