"""Sample Python file for testing the parser."""

import os
from pathlib import Path  # noqa: F401 — used by parser tests


class BaseService:
    """A base service class."""

    def __init__(self, name: str):
        self.name = name

    def start(self) -> None:
        print(f"Starting {self.name}")


class AuthService(BaseService):
    """Authentication service."""

    def __init__(self, name: str, secret: str):
        super().__init__(name)
        self.secret = secret

    def authenticate(self, token: str) -> bool:
        return self._validate_token(token)

    def _validate_token(self, token: str) -> bool:
        return token == self.secret


def create_auth_service() -> AuthService:
    secret = os.environ.get("SECRET", "default")
    return AuthService("auth", secret)


def process_request(service: AuthService, token: str) -> dict:
    if service.authenticate(token):
        return {"status": "ok"}
    return {"status": "denied"}


def _log_action(func):
    """Simple decorator."""
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)
    return wrapper


@_log_action
def guarded_process(service: AuthService, token: str) -> dict:
    return process_request(service, token)
