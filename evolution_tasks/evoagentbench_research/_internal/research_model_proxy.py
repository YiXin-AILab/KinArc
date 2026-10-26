"""Host-owned, trial-scoped fixed-model proxy for untrusted research candidates.

Scope files and request receipts contain only authorization digests, public counters
and opaque request hashes. Keep the entire scopes directory outside candidate mounts.
"""

from __future__ import annotations

import argparse
import asyncio
import codecs
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import httpx
    from fastapi import FastAPI, Request
    from starlette.responses import Response
    from starlette.types import Receive, Scope, Send

_MAX_BODY_BYTES = 4 * 1024 * 1024
_JOB_TAG = re.compile(r"[0-9a-f]{32}\Z")
_TOKEN = re.compile(r"([0-9a-f]{32}):[0-9a-f]{32}\Z")
_OUTPUT_CAPS = {
    "max_tokens",
    "max_completion_tokens",
    "max_output_tokens",
    "max_new_tokens",
}
_TRANSPORT_OPTIONS = {
    "api_key",
    "api_base",
    "base_url",
    "api_url",
    "endpoint",
    "api_version",
    "custom_llm_provider",
    "deployment_id",
    "headers",
    "extra_headers",
    "organization",
    "project",
}


def _checked_tag(job_tag: str) -> str:
    if not _JOB_TAG.fullmatch(job_tag):
        raise ValueError("job_tag must contain exactly 32 lowercase hex characters")
    return job_tag


def _write_json(
    path: Path, payload: dict[str, Any], *, exclusive: bool = False
) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=True, allow_nan=False)
            file.flush()
            os.fsync(file.fileno())
        if exclusive:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def register_scope(
    scopes_dir: Path, job_tag: str, lifetime_seconds: float = 1260
) -> str:
    """Create a private authorization scope and return its one-time plaintext token.

    Args:
        scopes_dir: Host-private storage, never mounted in a candidate container.
        job_tag: Unique lowercase 32-hex trial identity.
        lifetime_seconds: Token validity from registration; defaults to 21 minutes.

    Returns:
        Bearer credential whose digest alone is persisted.

    Raises:
        ValueError: The identity is invalid or was already registered.
    """
    scope_dir = scopes_dir / _checked_tag(job_tag)
    scopes_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    scopes_dir.chmod(0o700)
    scope_dir.mkdir(exist_ok=True, mode=0o700)
    scope_dir.chmod(0o700)
    requests_dir = scope_dir / "requests"
    requests_dir.mkdir(exist_ok=True, mode=0o700)
    requests_dir.chmod(0o700)
    token = f"{job_tag}:{uuid.uuid4().hex}"
    now = time.time()
    try:
        _write_json(
            scope_dir / "scope.json",
            {
                "job_tag": job_tag,
                "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                "created_at": now,
                "expires_at": now + lifetime_seconds,
            },
            exclusive=True,
        )
    except FileExistsError:
        raise ValueError("job_tag is already registered") from None
    return token


def revoke_scope(scopes_dir: Path, job_tag: str) -> None:
    """Atomically revoke future authorization without modifying request receipts.

    The server observes revocation and cancels active requests within its 100 ms
    polling interval. Their receipts still finalize; the caller must wait for pending
    receipts to drain. Repeated revocation, including an absent scope, is harmless.

    Args:
        scopes_dir: Host-private scope/receipt directory.
        job_tag: Lowercase 32-hex trial identity to revoke.
    """
    scope_file = scopes_dir / _checked_tag(job_tag) / "scope.json"
    try:
        scope = json.loads(scope_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    scope["expires_at"] = 0
    _write_json(scope_file, scope)


def read_scope_usage(scopes_dir: Path, job_tag: str) -> dict[str, Any]:
    """Sum atomic per-request receipts, retaining unknown or incomplete costs.

    The token totals are observed lower bounds when ``partial`` is true. A request
    without observed usage keeps null token counts in its receipt, never a fabricated
    zero. Runtime-reported costs should be reconciled with this total using ``max``.
    """
    aggregate: dict[str, Any] = {
        "requests": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "partial": False,
        "partial_requests": 0,
        "missing_usage_requests": 0,
        "pending_requests": 0,
        "failed_requests": 0,
        "cancelled_requests": 0,
    }
    requests_dir = scopes_dir / _checked_tag(job_tag) / "requests"
    for path in requests_dir.glob("*.json"):
        receipt = json.loads(path.read_text(encoding="utf-8"))
        aggregate["requests"] += 1
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if isinstance(receipt.get(field), int):
                aggregate[field] += receipt[field]
        if receipt.get("partial", True):
            aggregate["partial"] = True
            aggregate["partial_requests"] += 1
        if receipt.get("total_tokens") is None:
            aggregate["missing_usage_requests"] += 1
        status = receipt.get("status")
        aggregate["pending_requests"] += status == "pending"
        aggregate["cancelled_requests"] += status == "cancelled"
        aggregate["failed_requests"] += status not in {
            "pending",
            "success",
            "cancelled",
        }
    return aggregate


def _authenticate(scopes_dir: Path, authorization: str | None) -> str:
    from fastapi import HTTPException

    denied = HTTPException(403, "Invalid or expired model proxy scope")
    if not authorization or not authorization.startswith("Bearer "):
        raise denied
    token = authorization.removeprefix("Bearer ")
    match = _TOKEN.fullmatch(token)
    if match is None:
        raise denied
    job_tag = match.group(1)
    try:
        scope = json.loads((scopes_dir / job_tag / "scope.json").read_text())
        digest = scope["token_sha256"]
        expires_at = scope["expires_at"]
        valid = (
            isinstance(digest, str)
            and isinstance(expires_at, int | float)
            and time.time() < expires_at
            and hmac.compare_digest(digest, hashlib.sha256(token.encode()).hexdigest())
        )
    except (OSError, ValueError, KeyError, TypeError):
        raise denied from None
    if not valid:
        raise denied
    return job_tag


async def _request_payload(request: Request, model: str) -> dict[str, Any]:
    from fastapi import HTTPException

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_BODY_BYTES:
            raise HTTPException(413, "Model request exceeds 4 MiB")
        body.extend(chunk)
    try:
        payload = json.loads(body)
        json.dumps(payload, allow_nan=False)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise HTTPException(400, "Invalid model request JSON") from None
    if not isinstance(payload, dict):
        raise HTTPException(400, "Model request must be a JSON object")
    requested_model = payload.get("model")
    if not isinstance(requested_model, str) or requested_model not in {
        model,
        f"openai/{model}",
    }:
        raise HTTPException(400, "Only the fixed evaluation model is available")
    if not isinstance(payload.get("stream", False), bool):
        raise HTTPException(400, "stream must be a boolean")
    payload["model"] = model
    for field in _OUTPUT_CAPS | _TRANSPORT_OPTIONS:
        payload.pop(field, None)
    if payload.get("stream"):
        options = payload.get("stream_options", {})
        if not isinstance(options, dict):
            raise HTTPException(400, "stream_options must be a JSON object")
        payload["stream_options"] = {**options, "include_usage": True}
    return payload


@dataclass
class _Receipt:
    request_id: str
    request_hash: str
    started_at: float
    stream: bool
    status: str = "pending"
    completed_at: float | None = None
    upstream_status: int | None = None
    error_kind: str | None = None
    cancel_reason: str | None = None
    close_error_kind: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    total_tokens_inferred: bool = False
    partial: bool = True
    done_seen: bool = False
    downstream_stream_started: bool = False

    def observe(self, payload: object) -> None:
        if not isinstance(payload, dict) or not isinstance(payload.get("usage"), dict):
            return
        usage = payload["usage"]
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                previous = getattr(self, field)
                setattr(
                    self, field, value if previous is None else max(previous, value)
                )
                if field == "total_tokens":
                    self.total_tokens_inferred = False
        if (
            (self.total_tokens is None or self.total_tokens_inferred)
            and self.prompt_tokens is not None
            and self.completion_tokens is not None
        ):
            self.total_tokens = self.prompt_tokens + self.completion_tokens
            self.total_tokens_inferred = True

    def finish(self) -> None:
        self.completed_at = time.time()
        self.partial = (
            self.status != "success"
            or self.total_tokens is None
            or (self.stream and not self.done_seen)
        )


class _SSEUsage:
    """Incrementally inspect SSE events without retaining model text or thoughts."""

    def __init__(self, receipt: _Receipt) -> None:
        self.receipt = receipt
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.line: list[str] = []
        self.data: list[str] = []
        self.event_bytes = 0
        self.discard = False
        self.skip_lf = False

    def feed(self, chunk: bytes) -> None:
        decoded = self.decoder.decode(chunk)
        if not decoded:
            return
        if self.skip_lf and decoded.startswith("\n"):
            decoded = decoded[1:]
        self.skip_lf = decoded.endswith("\r")
        fragments = re.split(r"\r\n|\r|\n", decoded)
        for index, fragment in enumerate(fragments):
            self.event_bytes += len(fragment)
            if self.event_bytes > _MAX_BODY_BYTES:
                self.discard = True
                self.data.clear()
                self.line.clear()
            if not self.discard:
                self.line.append(fragment)
            if index != len(fragments) - 1:
                self._complete_line()

    def _complete_line(self) -> None:
        line, self.line = "".join(self.line), []
        if not line:
            self._complete_event()
            self.event_bytes = 0
            self.discard = False
        elif line.startswith("data:") and not self.discard:
            self.data.append(line[5:].removeprefix(" "))

    def _complete_event(self) -> None:
        data, self.data = "\n".join(self.data), []
        if self.discard or not data:
            return
        if data.strip() == "[DONE]":
            self.receipt.done_seen = True
            return
        try:
            self.receipt.observe(json.loads(data))
        except ValueError:
            return


async def _persist(path: Path, receipt: _Receipt) -> None:
    await asyncio.to_thread(_write_json, path, asdict(receipt))


def _scope_is_active(scope_file: Path) -> bool:
    try:
        scope = json.loads(scope_file.read_text(encoding="utf-8"))
        return bool(time.time() < scope["expires_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return False


async def _watch_request(
    scope_file: Path,
    request: Request,
    receipt: _Receipt,
    owner: asyncio.Task[Any],
) -> None:
    while True:
        await asyncio.sleep(0.1)
        if not await asyncio.to_thread(_scope_is_active, scope_file):
            receipt.cancel_reason = "scope_revoked_or_expired"
        elif not receipt.downstream_stream_started and await request.is_disconnected():
            receipt.cancel_reason = "client_disconnect"
        else:
            continue
        receipt.status = "cancelled"
        owner.cancel()
        return


def _start_watch(
    scopes_dir: Path,
    job_tag: str,
    request: Request,
    receipt: _Receipt,
    active: dict[str, set[asyncio.Task[Any]]],
) -> Callable[[], Awaitable[None]]:
    owner = asyncio.current_task()
    if owner is None:
        raise RuntimeError("Model proxy request requires an asyncio task")
    active.setdefault(job_tag, set()).add(owner)
    watcher = asyncio.create_task(
        _watch_request(scopes_dir / job_tag / "scope.json", request, receipt, owner)
    )

    async def stop() -> None:
        watcher.cancel()
        with suppress(asyncio.CancelledError):
            await watcher
        members = active.get(job_tag)
        if members is not None:
            members.discard(owner)
            if not members:
                active.pop(job_tag, None)

    return stop


async def _close_and_persist(
    response: httpx.Response | None, path: Path, receipt: _Receipt
) -> None:
    import httpx

    try:
        if response is not None:
            async with asyncio.timeout(15):
                await response.aclose()
    except (httpx.HTTPError, TimeoutError) as error:
        # Cleanup failures cannot replace cancellation or expose upstream details.
        receipt.close_error_kind = type(error).__name__
        if receipt.status == "success":
            receipt.status = "cleanup_error"
    except Exception as error:
        receipt.close_error_kind = type(error).__name__
        if receipt.status != "cancelled":
            receipt.status = "cleanup_error"
            raise RuntimeError("Model upstream cleanup failed") from None
    finally:
        receipt.finish()
        await _persist(path, receipt)


def _streaming_response(
    response: httpx.Response,
    path: Path,
    receipt: _Receipt,
    stop_watch: Callable[[], Awaitable[None]],
) -> Response:
    import httpx
    from starlette.requests import ClientDisconnect
    from starlette.responses import StreamingResponse

    parser = _SSEUsage(receipt)

    async def chunks() -> AsyncIterator[bytes]:
        try:
            async for chunk in response.aiter_bytes():
                parser.feed(chunk)
                yield chunk
            receipt.status = "success"
        except asyncio.CancelledError:
            receipt.status = "cancelled"
            raise
        except Exception as error:
            receipt.status = (
                "timeout"
                if isinstance(error, httpx.TimeoutException)
                else "stream_error"
            )
            receipt.error_kind = type(error).__name__
            raise RuntimeError("Model upstream stream failed") from None

    class ObservedResponse(StreamingResponse):
        """Own closure and accounting even when ASGI send/disconnect fails."""

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            # Once streaming starts Starlette owns the sole ASGI receive listener.
            # Before upstream headers arrive the watcher must also detect disconnects.
            receipt.downstream_stream_started = True
            try:
                await super().__call__(scope, receive, send)
            except (asyncio.CancelledError, ClientDisconnect, OSError):
                receipt.status = "cancelled"
                raise
            finally:
                if receipt.status == "pending":
                    receipt.status = "cancelled"
                try:
                    await asyncio.shield(_close_and_persist(response, path, receipt))
                finally:
                    await stop_watch()

    return ObservedResponse(
        chunks(),
        status_code=response.status_code,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


async def _dispatch(
    client: httpx.AsyncClient,
    endpoint: str,
    api_key: str,
    payload: dict[str, Any],
    path: Path,
    receipt: _Receipt,
    stop_watch: Callable[[], Awaitable[None]],
) -> Response:
    import httpx
    from starlette.responses import JSONResponse, Response

    upstream: httpx.Response | None = None
    streaming = False
    try:
        request = client.build_request(
            "POST",
            endpoint,
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept-Encoding": "identity",
            },
        )
        upstream = await client.send(request, stream=True)
        receipt.upstream_status = upstream.status_code
        if not upstream.is_success:
            receipt.status = "upstream_error"
            return JSONResponse(
                {"detail": "Upstream model request failed"},
                status_code=upstream.status_code,
            )
        if receipt.stream:
            result = _streaming_response(upstream, path, receipt, stop_watch)
            streaming = True
            return result
        body = await upstream.aread()
        with suppress(ValueError, UnicodeDecodeError):
            receipt.observe(json.loads(body))
        receipt.status = "success"
        return Response(
            body,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
        )
    except asyncio.CancelledError:
        receipt.status = "cancelled"
        raise
    except httpx.HTTPError as error:
        receipt.error_kind = type(error).__name__
        is_timeout = isinstance(error, httpx.TimeoutException)
        receipt.status = "timeout" if is_timeout else "upstream_error"
        return JSONResponse(
            {"detail": "Upstream model request failed"},
            status_code=504 if is_timeout else 502,
        )
    except Exception as error:
        receipt.error_kind = type(error).__name__
        receipt.status = "upstream_error"
        # The ASGI owner may log this stack; provider exception messages can
        # contain bodies or credentials and must never reach that logger.
        raise RuntimeError("Model upstream request failed") from None
    finally:
        if not streaming:
            try:
                await asyncio.shield(_close_and_persist(upstream, path, receipt))
            finally:
                await stop_watch()


def create_app(
    scopes_dir: Path,
    upstream_url: str,
    api_key: str,
    model: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the proxy with fixed host-owned endpoint, credential and model.

    Args:
        scopes_dir: Private host scope/receipt directory.
        upstream_url: OpenAI API base URL or complete chat-completions endpoint.
        api_key: Credential used only for the configured upstream.
        model: Fixed model name, optionally prefixed with ``openai/``.
        transport: Optional upstream transport for offline contract verification.

    Returns:
        A FastAPI application exposing only scoped chat completions.
    """
    import httpx
    from fastapi import FastAPI, Request

    model = model.removeprefix("openai/")
    if not model or not api_key:
        raise ValueError("Host model and credential are required")
    url = httpx.URL(upstream_url)
    if url.scheme not in {"http", "https"} or not url.host or url.query or url.fragment:
        raise ValueError("Invalid host upstream API URL")
    endpoint = upstream_url.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    client = httpx.AsyncClient(
        transport=transport or httpx.AsyncHTTPTransport(retries=0),
        timeout=httpx.Timeout(1200, connect=15, write=30, pool=15),
        follow_redirects=False,
        trust_env=False,
    )
    active: dict[str, set[asyncio.Task[Any]]] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await client.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.active_scope_tasks = active

    async def chat_completions(request: Request) -> Response:
        job_tag = await asyncio.to_thread(
            _authenticate, scopes_dir, request.headers.get("authorization")
        )
        payload = await _request_payload(request, model)
        request_id = uuid.uuid4().hex
        receipt = _Receipt(
            request_id=request_id,
            request_hash=hashlib.sha256(
                json.dumps(payload, sort_keys=True).encode()
            ).hexdigest(),
            started_at=time.time(),
            stream=payload.get("stream", False),
        )
        path = scopes_dir / job_tag / "requests" / f"{request_id}.json"
        await _persist(path, receipt)
        stop_watch = _start_watch(scopes_dir, job_tag, request, receipt, active)
        return await _dispatch(
            client, endpoint, api_key, payload, path, receipt, stop_watch
        )

    # FastAPI resolves route annotations in module globals, while the optional
    # Request dependency is intentionally imported only when building the server.
    chat_completions.__annotations__["request"] = Request
    app.add_api_route(
        "/v1/chat/completions", chat_completions, methods=["POST"], response_model=None
    )
    return app


def main() -> None:
    """Serve the host proxy using evaluation environment credentials only."""
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=19118)
    parser.add_argument("--scopes-dir", type=Path, required=True)
    arguments = parser.parse_args()
    required = ("EVALUATION_API_BASE", "EVALUATION_API_KEY", "EVALUATION_MODEL")
    if any(not os.environ.get(name) for name in required):
        parser.error(
            "EVALUATION_API_BASE, EVALUATION_API_KEY and EVALUATION_MODEL required"
        )
    app = create_app(
        arguments.scopes_dir,
        os.environ["EVALUATION_API_BASE"],
        os.environ["EVALUATION_API_KEY"],
        os.environ["EVALUATION_MODEL"],
    )
    uvicorn.run(app, host="127.0.0.1", port=arguments.port, access_log=False)


if __name__ == "__main__":
    main()
