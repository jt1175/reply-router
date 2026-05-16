"""Smartlead reply webhook handler — thin FastAPI route delegating to orchestrator."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from reply_router.config import load_client_config, ConfigError

app = FastAPI(title="reply-router", version="0.1.0")
logger = logging.getLogger("api.replies")

# Default to repo-root /clients; tests override with REPLY_ROUTER_CLIENTS_DIR.
# Read at call time so monkeypatch in tests takes effect even after module import.
_DEFAULT_CLIENTS_DIR = "clients"


def _load_client(client_id: str):
    clients_dir = Path(os.environ.get("REPLY_ROUTER_CLIENTS_DIR", _DEFAULT_CLIENTS_DIR))
    config_path = clients_dir / f"{client_id}.json"
    try:
        return load_client_config(config_path)
    except (ConfigError, FileNotFoundError) as exc:
        logger.error("config load failed for client_id=%s err=%s", client_id, exc)
        raise HTTPException(
            status_code=500,
            detail="config_load_failed",
        ) from exc


def _check_auth(client_config, provided_secret: str) -> None:
    expected = os.environ.get(client_config.auth.router_secret_env, "")
    if not expected or provided_secret != expected:
        logger.warning("auth fail for client_id=%s", client_config.client_id)
        raise HTTPException(status_code=401, detail="unauthorized")


@app.post("/v1/clients/{client_id}/replies")
async def handle_reply(
    client_id: str,
    request: Request,
    x_router_secret: str = Header(default=""),
):
    client_config = _load_client(client_id)
    _check_auth(client_config, x_router_secret)
    payload = await request.json()
    # Stub: full orchestration lands in Task 4.1d–4.1h.
    # Loop check goes here first (Task 4.1b).
    from reply_router.orchestrator import process_reply, ReplyPayload
    rp = ReplyPayload.from_smartlead_webhook(payload)
    result = process_reply(client_config, rp, source="webhook")
    return JSONResponse(content=result.to_response(), status_code=result.http_status)
