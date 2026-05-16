"""Public health endpoint for external uptime monitors."""
from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import FastAPI

app = FastAPI(title="reply-router-health")


@app.get("/v1/health")
async def health():
    return {
        "status": "ok",
        "git_sha": os.environ.get("VERCEL_GIT_COMMIT_SHA", "unknown")[:12],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
