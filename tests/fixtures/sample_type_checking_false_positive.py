"""False-positive control: a local ``TYPE_CHECKING`` variable that is NOT
imported from ``typing`` must not mark calls as dead.

See: #576, #580.
"""


TYPE_CHECKING = False  # noqa: N816 -- local, not from typing


def live_under_local_tc():
    """This call should be LIVE -- TYPE_CHECKING is a local variable."""
    if TYPE_CHECKING:
        should_be_live()             # noqa: F821
