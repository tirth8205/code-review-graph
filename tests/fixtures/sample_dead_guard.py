"""Fixture for testing dead-guard detection on CALLS edges.

Contains calls under ``if False:``, ``if 0:``, ``if TYPE_CHECKING:``,
and live calls as controls.  The parser should:

* **omit** CALLS edges inside the consequence (true branch) of a dead
  guard -- those edges are never emitted at all.
* **keep** CALLS edges inside the else/elif branch of a dead guard --
  those branches execute when the condition is false.
* treat ``TYPE_CHECKING`` as the typing sentinel only when it is
  imported from ``typing``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from os.path import join as pjoin  # noqa: F401


def live_helper():
    pass


def caller():
    live_helper()                    # live -- no guard

    if False:
        dead_false_call()            # noqa: F821 -- dead consequence

    if 0:
        dead_zero_call()             # noqa: F821 -- dead consequence

    if TYPE_CHECKING:
        dead_tc_call()               # noqa: F821 -- dead consequence


def else_branch():
    """Calls in the else branch of ``if False:`` are LIVE."""
    if False:
        dead_in_if()                 # noqa: F821 -- dead consequence
    else:
        live_in_else()               # live -- else branch of if False


def elif_chain():
    """Calls in elif branches of ``if False:`` are LIVE."""
    if False:
        dead_elif_consequence()      # noqa: F821 -- dead
    elif some_condition():
        live_elif_call()             # live -- elif branch
    else:
        live_final_else()            # live -- else branch


def some_condition() -> bool:
    return True
