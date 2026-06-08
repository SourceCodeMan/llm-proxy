import asyncio
import json
import logging
from datetime import datetime

import httpx

from . import backends, db, utils

logger = logging.getLogger(__name__)
GANDALF = "gandalfthewhite"


async def _send_callback(url: str, job_id: int, response: dict, error: str):
    payload = {"job_id": job_id}
    if error:
        payload["error"] = error
    else:
        payload["response"] = response
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(url, json=payload)
    except Exception as e:
        logger.warning(f"Callback to {url} failed: {e}")


async def _process_job(job):
    job_id = job["id"]
    queued_at = job["created_at"]
    started_at = datetime.utcnow().isoformat()
    request = json.loads(job["request_json"])
    model_requested = request.get("model", "unknown")

    await db.mark_job_processing(job_id)
    logger.info(f"Processing job {job_id} (model={model_requested})")

    try:
        response, duration_ms = await backends.call_ollama(request)
        backend = "gpu"
        model_used = response.get("model", model_requested)
    except Exception as e:
        logger.warning(f"Job {job_id}: Ollama failed ({e}), falling back to Claude")
        try:
            response, duration_ms = await backends.call_claude(request)
            backend = "claude"
            model_used = response.get("model", backends.CLAUDE_MODEL)
        except Exception as e2:
            logger.error(f"Job {job_id}: Claude also failed: {e2}")
            await db.complete_job(
                job_id=job_id,
                response_json=None,
                backend="failed",
                model_requested=model_requested,
                model_used=None,
                duration_ms=0,
                queued_at=queued_at,
                started_at=started_at,
                request_json=job["request_json"],
                error=str(e2),
            )
            if job["callback_url"]:
                await _send_callback(job["callback_url"], job_id, None, str(e2))
            return

    await db.complete_job(
        job_id=job_id,
        response_json=json.dumps(response),
        backend=backend,
        model_requested=model_requested,
        model_used=model_used,
        duration_ms=duration_ms,
        queued_at=queued_at,
        started_at=started_at,
        request_json=job["request_json"],
    )
    logger.info(f"Job {job_id} done via {backend} in {duration_ms}ms")

    if job["callback_url"]:
        await _send_callback(job["callback_url"], job_id, response, None)


async def queue_worker():
    logger.info("Queue worker started")
    while True:
        try:
            flag = await db.get_flag()
            if flag == "send" and await utils.is_host_online(GANDALF):
                job = await db.get_next_pending_job()
                if job:
                    await _process_job(job)
                    continue  # skip sleep, grab next job immediately
        except Exception as e:
            logger.error(f"Worker loop error: {e}")

        await asyncio.sleep(5)
