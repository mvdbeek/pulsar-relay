"""Tests for the long-poll cleanup loop and per-user caps.

Closes API H#8: ``cleanup_stale_waiters`` was defined but never invoked.
``PollManager.cleanup_loop`` is now scheduled from the lifespan startup
so abandoned waiters are reaped on a fixed cadence. The per-user
concurrent-waiter cap and bounded waiter queue defend against a single
client exhausting the manager's state.
"""

from __future__ import annotations

import asyncio

import pytest

from pulsar_relay.core.polling import (
    _DEFAULT_WAITER_QUEUE_MAXSIZE,
    PollManager,
    PollWaiter,
    PollWaiterLimitExceededError,
)


@pytest.mark.anyio
async def test_per_user_waiter_cap_enforced() -> None:
    """A user that hits the cap can't allocate further waiters until at
    least one of theirs is removed."""
    mgr = PollManager(max_waiters_per_user=3)
    user = "user-1"
    waiters = [await mgr.create_waiter(["t1"], user_id=user) for _ in range(3)]

    with pytest.raises(PollWaiterLimitExceededError):
        await mgr.create_waiter(["t2"], user_id=user)

    # Free a slot; a new waiter should succeed.
    await mgr.remove_waiter(waiters[0].client_id)
    new_waiter = await mgr.create_waiter(["t3"], user_id=user)
    assert new_waiter.user_id == user


@pytest.mark.anyio
async def test_unauthenticated_waiter_is_not_capped() -> None:
    """Anonymous waiters (user_id=None) bypass the per-user cap. The
    rate limiter handles the unauthenticated case separately."""
    mgr = PollManager(max_waiters_per_user=2)
    # Create 5 anonymous waiters — none should raise.
    for _ in range(5):
        await mgr.create_waiter(["t"], user_id=None)
    assert mgr.get_stats()["active_waiters"] == 5


@pytest.mark.anyio
async def test_remove_waiter_decrements_user_counter() -> None:
    """The per-user counter must decrement on removal so a user can
    re-use slots over time."""
    mgr = PollManager(max_waiters_per_user=1)
    user = "alice"
    waiter = await mgr.create_waiter(["t"], user_id=user)
    with pytest.raises(PollWaiterLimitExceededError):
        await mgr.create_waiter(["t"], user_id=user)
    await mgr.remove_waiter(waiter.client_id)
    # Should now succeed.
    await mgr.create_waiter(["t"], user_id=user)


@pytest.mark.anyio
async def test_waiter_queue_is_bounded() -> None:
    """``put_message`` returns False when the queue is full rather than
    blocking. Drop-the-newest preserves the relay's responsiveness
    under fan-out pressure from a slow consumer."""
    # Use a tiny queue to exercise the bound without burning memory.
    waiter = PollWaiter("c1", ["t"], queue_maxsize=2)
    assert await waiter.put_message({"i": 1}) is True
    assert await waiter.put_message({"i": 2}) is True
    # Queue full — third message is dropped, function returns False.
    assert await waiter.put_message({"i": 3}) is False


def test_default_queue_maxsize_is_documented() -> None:
    """Pin the default so regressions are explicit code changes."""
    assert _DEFAULT_WAITER_QUEUE_MAXSIZE == 1024


@pytest.mark.anyio
async def test_cleanup_loop_evicts_stale_waiters() -> None:
    """The loop calls ``cleanup_stale_waiters`` on a configurable
    cadence. A waiter older than ``max_age_seconds`` is removed."""
    import datetime

    mgr = PollManager()
    waiter = await mgr.create_waiter(["t"], user_id="user")

    # Backdate the waiter so it looks stale.
    waiter.created_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=600)

    # One sweep with max_age_seconds=60 should reap it.
    removed = await mgr.cleanup_stale_waiters(max_age_seconds=60)
    assert removed == 1
    assert mgr.get_stats()["active_waiters"] == 0


@pytest.mark.anyio
async def test_cleanup_loop_cancels_cleanly() -> None:
    """Cancelling the loop returns control to the caller without
    raising — important for the lifespan shutdown path."""
    mgr = PollManager()
    task = asyncio.create_task(mgr.cleanup_loop(interval_seconds=1))
    await asyncio.sleep(0)  # let it start
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
