"""Worker threads that lease due tickets and run one workflow step each."""
from __future__ import annotations

import os
import socket
import threading
import time
import traceback

from .store import Store
from .workflow import Context, advance

LEASE = 300          # seconds; renewed by a heartbeat while the step runs
HEARTBEAT = 60
BACKOFF = (60, 300, 900)
MAX_ERRORS = 3


class LeaseLost(RuntimeError):
    """Another worker took the ticket over; this one stops without touching it."""


class Scheduler:
    def __init__(self, ctx: Context, workers: int = 1, poll: float = 5):
        self.ctx, self.workers, self.poll = ctx, workers, poll
        self.stop = threading.Event()
        self.threads: list[threading.Thread] = []
        self.status: dict[str, dict] = {}

    def start(self) -> None:
        for index in range(self.workers):
            thread = threading.Thread(target=self.loop, args=(f"{socket.gethostname()}:{os.getpid()}:{index}",), daemon=True)
            thread.start()
            self.threads.append(thread)

    def shutdown(self, timeout: float = 5) -> None:
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout)

    def loop(self, owner: str) -> None:
        while not self.stop.is_set():
            try:
                worked = self.once(owner)
            except Exception:  # the loop must survive a broken store call; the next poll retries
                traceback.print_exc()
                worked = False
            if not worked:
                self.stop.wait(self.poll)

    def once(self, owner: str) -> bool:
        store: Store = self.ctx.store
        ticket = store.claim(owner, LEASE)
        if ticket is None:
            self.status[owner] = {"idle": True, "at": time.time()}
            return False
        self.status[owner] = {"ticket": ticket["id"], "state": ticket["state"], "since": time.time()}
        beat = threading.Event()

        def heartbeat():
            while not beat.wait(HEARTBEAT):
                store.renew(ticket["id"], owner, LEASE)

        threading.Thread(target=heartbeat, daemon=True).start()

        def renew():
            if not store.renew(ticket["id"], owner, LEASE):
                raise LeaseLost(f"{ticket['id']} lease was lost; another worker may hold it")

        ctx = Context(self.ctx.home, store, self.ctx.repos, self.ctx.registry, owner=owner, renew=renew)
        try:
            advance(ctx, ticket)
            fresh = store.get(ticket["id"])
            if fresh["lease_owner"] == owner:  # the step ended without a transition (waiting for a person)
                store.release(ticket["id"], owner)
            if fresh["data"].get("errors"):
                store.merge_data(ticket["id"], errors=0)
        except LeaseLost as exc:
            store.event(ticket["id"], "lease_lost", owner, {"error": str(exc)})
        except Exception as exc:
            self.failed(ticket, owner, exc)
        finally:
            beat.set()
        return True

    def failed(self, ticket: dict, owner: str, exc: Exception) -> None:
        """An infrastructure error (model server down, disk, GitHub outage): retry with backoff, then stop."""
        store = self.ctx.store
        errors = (store.get(ticket["id"])["data"].get("errors") or 0) + 1
        message = f"{type(exc).__name__}: {exc}"
        store.event(ticket["id"], "step_error", owner, {"state": ticket["state"], "error": message, "attempt": errors,
                                                        "trace": traceback.format_exc()[-2000:]})
        store.merge_data(ticket["id"], errors=errors)
        if errors >= MAX_ERRORS and ticket["state"] in ("ready", "merging"):
            # Publication or merge keeps failing: hand it to a person instead of retrying forever.
            if ticket["state"] == "ready":
                store.merge_data(ticket["id"], publish=False)
                store.release(ticket["id"], owner, error=f"publication stopped after {errors} errors: {message}")
            else:
                store.release(ticket["id"], owner)
                store.transition(ticket["id"], "awaiting_merge", owner, "merge_stopped", {"error": message},
                                 error=f"merge stopped after {errors} errors: {message}")
            return
        if errors >= MAX_ERRORS and ticket["state"] in ("new", "accepted"):
            store.release(ticket["id"], owner)
            failed_at = "intake" if ticket["state"] == "new" else "develop"
            store.transition(ticket["id"], "failed", owner, "step_failed", {"error": message},
                             data={**store.get(ticket["id"])["data"], "failed_at": failed_at}, error=f"stopped after {errors} errors: {message}")
            return
        delay = BACKOFF[min(errors, len(BACKOFF)) - 1]
        store.release(ticket["id"], owner, error=f"{message} (retrying in {delay // 60} min)", not_before=time.time() + delay)
