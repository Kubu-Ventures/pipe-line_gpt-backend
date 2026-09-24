from __future__ import annotations

from typing import Any

DEFAULT_PASSWORD = "Passw0rd!2026"


def auth_headers(user: Any) -> dict[str, str]:
    from app.middleware.auth import create_access_token

    return {"Authorization": f"Bearer {create_access_token(str(user.id), user.role)}"}
