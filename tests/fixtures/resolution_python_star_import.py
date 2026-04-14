"""Fixture: star import followed by call to imported symbol.

Pain point: `from sample_python import *; create_auth_service()` does NOT
resolve because star imports don't populate import_map.
"""

from sample_python import *  # noqa: F403


def make_service():
    return create_auth_service()  # noqa: F405
