"""Fixture that imports and calls functions from sample_python."""

from sample_python import create_auth_service


def setup_and_run():
    service = create_auth_service()
    return service
