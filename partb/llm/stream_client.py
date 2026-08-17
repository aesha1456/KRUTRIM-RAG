"""Stream tokens from the unified load balancer (/generate), with LiteLLM
as fallback if the LB itself is unreachable."""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from partb.logger import time_it, async_time_it, logger

import time

from partb.config import (
    LITELLM_API_KEY,
    LITELLM_BASE_URL,
    OLLAMA_LB_URL,
)


@time_it
def _prompt_from_messages(messages: list[dict[str, str]]) -> str:
    """Ollama /api/generate expects a single prompt string."""
    parts = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            parts.append(f"System:\n{content}")
        elif role == "user":
            parts.append(f"User:\n{content}")
        else:
            parts.append(f"Assistant:\n{content}")
    return "\n\n".join(parts)


@async_time_it
async def stream_llm(
    messages: list[dict[str, str]],
    mode: str,
    cfg: dict[str, Any],
) -> AsyncIterator[dict]:
    timeout = cfg.get("llm_timeout_s", 600.0)
    prompt = _prompt_from_messages(messages)
    litellm_model = cfg["litellm_model"]
    model = cfg.get("ollama_model") or litellm_model

    # Try the unified LB first
    try:
        logger.info("[LLM] Starting stream | provider=ollama_lb | mode=%s", mode)
        async for ev in _stream_via_lb(prompt, "krag", mode, model):
            yield ev
        return
    except Exception as e:
        logger.warning("[LB] Failed, falling back to LiteLLM: %s", e)

    # Fallback to LiteLLM
    try:
        logger.info("[LLM] Starting stream | provider=litellm | model=%s | mode=%s", litellm_model, mode)
        async for ev in _stream_litellm(messages, timeout, litellm_model):
            yield ev
        return
    except Exception as e:
        logger.warning("[LITELLM] All fallbacks exhausted, last error: %s", e)
        yield {"type": "error", "message": f"All LLM backends failed: {e}"}


@async_time_it
async def _stream_via_lb(
    prompt: str,
    project: str,
    mode: str,
    model: str,
) -> AsyncIterator[dict]:
    """Single call to the unified LB's /generate. The LB owns allocation
    and queueing — this client just sends the request and reads the
    resulting NDJSON stream.

    Raises an exception on recoverable errors (timeout, upstream error,
    LB queue timeout) so the caller can fall back to LiteLLM. Only
    truly fatal errors (HTTP 4XX) are yielded as error events."""
    url = f"{OLLAMA_LB_URL.rstrip('/')}/generate"
    t0 = time.perf_counter()
    t_first_token = None
    token_count = 0
    char_count = 0

    body = {
        "project": project,
        "mode": mode,
        "prompt": prompt,
        "options": {"stream": True},
    }

    # Keep connection establishment bounded, but do not impose a response
    # inactivity/total timeout on the LB stream. Long generations may be slow
    # before the first token or between tokens. The LB still limits queue wait
    # time separately with MAX_WAIT_SEC.
    lb_timeout = httpx.Timeout(
        connect=10.0,
        read=None,
        write=None,
        pool=None,
    )
    async with httpx.AsyncClient(timeout=lb_timeout) as client:
        try:
            async with client.stream("POST", url, json=body) as resp:
                if resp.status_code != 200:
                    err = await resp.aread()
                    err_text = err.decode(errors="replace")[:500]
                    logger.error("[LB] HTTP error | status=%s | body=%s", resp.status_code, err_text)
                    # 4XX = fatal client error (unknown project, bad mode), don't retry
                    if 400 <= resp.status_code < 500:
                        yield {"type": "error", "message": f"LB HTTP {resp.status_code}: {err_text}"}
                        return
                    # 5XX = server error, raise so caller falls back to LiteLLM
                    raise Exception(f"LB HTTP {resp.status_code}: {err_text}")

                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    # Recoverable LB error — raise so caller falls back to LiteLLM.
                    # The outer `except Exception` handler logs the full traceback.
                    if data.get("type") == "error":
                        msg = data.get("message", "Unknown LB error")
                        raise Exception(f"LB streaming error: {msg}")

                    # Otherwise it's a raw Ollama /api/generate line
                    token = data.get("response") or ""
                    if token:
                        token_count += 1
                        char_count += len(token)
                        if token_count == 1:
                            t_first_token = time.perf_counter()
                            logger.info("[LB] First token | cold_startup_time=%.2fs", t_first_token - t0)
                        yield {"type": "token", "content": token}
                    if data.get("done"):
                        break

                t_end = time.perf_counter()
                if t_first_token:
                    logger.info(
                        "[LB] Stream complete | tokens=%s | chars=%s | cold_startup_time=%.2fs | response_time=%.2fs",
                        token_count, char_count, t_first_token - t0, t_end - t_first_token,
                    )
                else:
                    logger.info("[LB] Stream complete | tokens=%s | chars=%s | elapsed=%.2fs", token_count, char_count, t_end - t0)
                duration = t_end - t0
                yield {
                    "type": "metrics",
                    "metrics": {
                        "model": model,
                        "tokens": token_count,
                        "chars": char_count,
                        "duration": round(duration, 3),
                        "tps": round(token_count / duration, 3) if duration > 0 else 0.0,
                        "ttf": round(t_first_token - t0, 3) if t_first_token else 0.0,
                    },
                }

        except httpx.TimeoutException:
            logger.error("[LB] Connection timed out after %.2fs", time.perf_counter() - t0)
            raise
        except Exception as e:
            logger.exception("[LB] Stream error")
            raise


@async_time_it
async def _stream_litellm(
    messages: list[dict[str, str]],
    timeout: float,
    model: str,
) -> AsyncIterator[dict]:
    url = f"{LITELLM_BASE_URL}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if LITELLM_API_KEY:
        headers["Authorization"] = f"Bearer {LITELLM_API_KEY}"

    body = {
        "model": model,
        "messages": messages,
        "stream": True,
        "temperature": 0.2,
    }

    t0 = time.perf_counter()
    t_first_token = None
    token_count = 0
    char_count = 0

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
        try:
            async with client.stream("POST", url, headers=headers, json=body) as resp:
                if resp.status_code != 200:
                    err = await resp.aread()
                    yield {
                        "type": "error",
                        "message": f"LLM HTTP {resp.status_code}: {err.decode(errors='replace')[:500]}",
                    }
                    return
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].lstrip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    content = delta.get("content") or ""
                    if content:
                        token_count += 1
                        char_count += len(content)
                        if token_count == 1:
                            t_first_token = time.perf_counter()
                        yield {"type": "token", "content": content}
                duration = time.perf_counter() - t0
                yield {
                    "type": "metrics",
                    "metrics": {
                        "model": model,
                        "tokens": token_count,
                        "chars": char_count,
                        "duration": round(duration, 3),
                        "tps": round(token_count / duration, 3) if duration > 0 else 0.0,
                        "ttf": round(t_first_token - t0, 3) if t_first_token else 0.0,
                    },
                }
        except httpx.TimeoutException:
            yield {"type": "error", "message": f"LLM timeout after {timeout}s"}
        except Exception as e:
            yield {"type": "error", "message": f"LLM stream error: {e}"}

