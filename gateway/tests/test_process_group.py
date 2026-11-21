"""The MLflow fixture's teardown leaves no child process behind."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time

from conftest import stop_process_group

PARENT = textwrap.dedent(
    """
    import subprocess, sys, time
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    print(child.pid, flush=True)
    time.sleep(600)
    """
)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie awaiting its (dead) parent's reaper is gone for our purposes
    state = subprocess.run(
        ["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
    ).stdout.strip()
    return bool(state) and not state.startswith("Z")


def test_stopping_the_group_kills_grandchildren_too() -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", PARENT], stdout=subprocess.PIPE, text=True, start_new_session=True
    )
    assert process.stdout is not None
    child = int(process.stdout.readline())
    assert alive(child)
    stop_process_group(process, grace=10)
    deadline = time.monotonic() + 10
    while alive(child) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not alive(child), "a child of the leader survived the teardown"
    assert process.poll() is not None
    process.stdout.close()


def test_terminating_only_the_leader_would_leave_the_child() -> None:
    """The failure mode the helper fixes, so the test above is known to discriminate."""
    process = subprocess.Popen(
        [sys.executable, "-c", PARENT], stdout=subprocess.PIPE, text=True, start_new_session=True
    )
    assert process.stdout is not None
    child = int(process.stdout.readline())
    try:
        process.terminate()
        process.wait(timeout=10)
        assert alive(child)
    finally:
        os.killpg(process.pid, 9)
        process.stdout.close()
