"""Credentials for the `/internal/*` callers in tests (api/internal_auth.py).

The app mints them into `JBRAIN_INTERNAL_SECRETS_DIR` (a per-run temp dir, see conftest)
when its lifespan starts, so read them after entering the TestClient.
"""

from typing import cast

from fastapi import FastAPI

from jbrain.storage import InternalSecrets


def token(app: FastAPI, caller: str) -> str:
    value = cast(InternalSecrets, app.state.internal_secrets).read(caller)
    assert value, f"the app minted no {caller} token"
    return value.decode()


def wall_headers(app: FastAPI) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(app, 'wall')}"}


def enroll_headers(app: FastAPI) -> dict[str, str]:
    value = cast(InternalSecrets, app.state.enroll_secrets).read("wall-enroll")
    assert value, "the app minted no wall-enroll token"
    return {"Authorization": f"Bearer {value.decode()}"}


def mqtt_headers(app: FastAPI) -> dict[str, str]:
    return {"User-Agent": token(app, "mqtt")}
