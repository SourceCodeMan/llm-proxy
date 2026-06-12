"""
Whisper-compatible audio transcription proxy.

Routing:
  flag=send + gandalfthewhite online  → local Whisper GPU at gandalfthewhite:8001
  flag=hold / reject / offline        → OpenAI Whisper API (whisper-1)

Accepts the same multipart form as the OpenAI /v1/audio/transcriptions endpoint:
  file            - audio file (required)
  model           - ignored locally, whisper-1 used for OpenAI
  language        - optional ISO-639-1 code
  prompt          - optional context string
  response_format - json (default) | text | srt | vtt | verbose_json
  temperature     - optional float
"""
import logging
import os
from typing import Optional

import httpx
from fastapi import UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse

from . import db, utils

GANDALF = "gandalfthewhite"
WHISPER_URL = os.getenv("WHISPER_URL", "http://gandalfthewhite:8001")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_WHISPER_URL = "https://api.openai.com/v1/audio/transcriptions"

logger = logging.getLogger(__name__)


def _make_response(content, response_format: str):
    if isinstance(content, dict):
        return JSONResponse(content)
    if response_format in ("text", "srt", "vtt"):
        return PlainTextResponse(content)
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
        logger.info(f"[whisper] → GPU ({filename}, {len(audio_bytes)} bytes, flag={flag})")
        try:
            return await _call_local_whisper(
                audio_bytes, filename, content_type,
                language, prompt, response_format, temperature
            )
        except Exception as e:
            logger.warning(f"[whisper] GPU failed ({e}), falling back to OpenAI")

    logger.info(f"[whisper] → OpenAI ({filename}, {len(audio_bytes)} bytes, flag={flag}, online={online})")
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

    logger.info("[whisper] ✓ GPU transcription complete")
    return _make_response(
        r.json() if response_format not in ("text", "srt", "vtt") else r.text,
        response_format
    )


async def _call_openai_whisper(
    audio_bytes, filename, content_type,
    language, prompt, response_format, temperature
):
    if not OPENAI_API_KEY:
        logger.error("[whisper] OPENAI_API_KEY not set")
        return JSONResponse(
            {"error": "OPENAI_API_KEY not configured — cannot transcribe without GPU"},
            status_code=503,
        )

    files = {"file": (filename, audio_bytes, content_type)}
    data = {"model": "whisper-1", "response_format": response_format}
    if language:   data["language"] = language
    if prompt:     data["prompt"] = prompt
    if temperature is not None: data["temperature"] = str(temperature)

    async with httpx.AsyncClient(timeout=300.0) as client:
        r = await client.post(
            OPENAI_WHISPER_URL,
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            files=files,
            data=data,
        )
        r.raise_for_status()

    logger.info("[whisper] ✓ OpenAI transcription complete")
    return _make_response(
        r.json() if response_format not in ("text", "srt", "vtt") else r.text,
        response_format
    )
