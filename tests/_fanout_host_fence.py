"""Whether this host can fence a real fanout dispatch, for fixtures that are not about the fence.

`dispatch_fanout` refuses a unit it cannot place inside a proven write fence
unless the caller passes `allow_unconfined=True` (#1982). Windows has no
backend, and CI's Linux runners carry no bwrap, so a fixture that exercises
something else through the real runner -- session receipts, failure
diagnostics -- has to say so explicitly on those hosts. It does that by
passing `allow_unconfined=host_cannot_fence_fanout()`: off wherever the host
can fence, so the fixture still runs fenced there, and visible at the call
site wherever it cannot. Never pass a bare `True` from a fixture.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

from _local_package import load_local_package

load_local_package()

from omh.coding.fanout_confinement import prepare_fanout_filesystem_confinement  # noqa: E402


def host_cannot_fence_fanout() -> bool:
    """True when a fanout fence prepared here would not be enforced.

    Asks the same preparation the dispatcher runs -- backend, preflight and
    probe -- rather than re-deriving which platforms have a backend, so a
    Linux host with a bwrap whose probe fails answers the same as one without.
    """
    with TemporaryDirectory() as directory:
        confinement = prepare_fanout_filesystem_confinement(
            Path(directory), dict(os.environ), ((sys.executable,),)
        )
    return confinement.receipt.get("enforced") is not True
