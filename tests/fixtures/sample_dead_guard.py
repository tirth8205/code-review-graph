"""Fixture for testing dead-guard detection on CALLS edges.

Contains calls under ``if False:``, ``if 0:``, ``if TYPE_CHECKING:``,
and a live call as a control.  The parser should tag the dead ones with
``extra["reachable"] == False`` and leave the live call without the key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from os.path import join as pjoin  # noqa: F401


def live_helper():
    pass


def caller():
    live_helper()

    if False:
        dead_false_call()  # noqa: F821

    if 0:
        dead_zero_call()  # noqa: F821

    if TYPE_CHECKING:
        dead_tc_call()  # noqa: F821
