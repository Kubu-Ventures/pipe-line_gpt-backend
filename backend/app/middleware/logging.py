from __future__ import annotations

import json
import time
import uuid

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware


class StructuredLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = str(uuid.uuid4())
        start = time.perf_counter()

        response = await call_next(request)

        duration_ms = int((time.perf_counter() - start) * 1000)
        log_entry = {
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
            "client_ip": request.client.host if request.client else None,
        }
        print(json.dumps(log_entry))
        response.headers["X-Request-ID"] = request_id
        return response
