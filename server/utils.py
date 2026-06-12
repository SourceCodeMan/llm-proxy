import asyncio


async def is_host_online(hostname: str, port: int = 11434, timeout: float = 2.0) -> bool:
    """Check if host is reachable by attempting a TCP connection."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(hostname, port),
            timeout=timeout,
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False
