"""Expose only reserve activation, bound to loopback by the service command."""

import asyncio
import os
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .engine import ActivationError, Activator, Journal
from .native import NativeBackend, service_credentials

activator = Activator(NativeBackend(), Journal(os.environ.get("RESERVE_STATE_DIR", "/state")))


@asynccontextmanager
async def lifespan(_app):
    task = None
    if os.environ.get("RESERVE_RECONCILE_ENABLED") == "1":
        task = asyncio.create_task(activator.reconcile_loop(service_credentials, interval=30))
    yield
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/healthz")
async def health():
    return {"status": "ok", "service": "reserve-start", "intent_phases": activator.journal.phase_counts()}


@app.post("/api/user/{username}/reserve-start")
async def reserve_start(username: str, request: Request):
    credentials = {key: request.headers[key] for key in ("authorization", "x-api-key") if key in request.headers}
    try:
        result = await activator.activate(username, credentials)
    except ActivationError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=exc.code)
    state = result["_reserve_activation"]["state"]
    return JSONResponse(result, status_code=200 if state == "active" else 202)
