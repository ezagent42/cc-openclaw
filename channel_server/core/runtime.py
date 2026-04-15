"""Actor runtime — manages actor lifecycle, mailboxes, and per-actor message loops."""
from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Callable

from channel_server.core.actor import (
    Action,
    Actor,
    Message,
    Send,
    SpawnActor,
    StopActor,
    Transport,
    TransportSend,
    UpdateActor,
)
from channel_server.core.handler import get_handler

log = logging.getLogger(__name__)


class ActorRuntime:
    """Central runtime that manages actor lifecycle, mailboxes, and per-actor asyncio message loops."""

    def __init__(self) -> None:
        self.actors: dict[str, Actor] = {}
        self.mailboxes: dict[str, asyncio.Queue] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._stop_event = asyncio.Event()
        self._transport_handlers: dict[str, Callable] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def spawn(
        self,
        address: str,
        handler: str,
        *,
        tag: str = "",
        state: str = "active",
        parent: str | None = None,
        downstream: list[str] | None = None,
        transport: Transport | None = None,
        metadata: dict | None = None,
    ) -> Actor:
        """Create a new actor. Raises ValueError if address is already taken by a non-ended actor."""
        existing = self.actors.get(address)
        if existing is not None and existing.state != "ended":
            raise ValueError(f"Actor {address} already exists")

        actor = Actor(
            address=address,
            tag=tag,
            handler=handler,
            state=state,
            parent=parent,
            downstream=downstream or [],
            transport=transport,
            metadata=metadata or {},
        )
        self.actors[address] = actor
        self.mailboxes[address] = asyncio.Queue()

        # If runtime is already running, start the actor loop immediately.
        if not self._stop_event.is_set() and state == "active":
            self._maybe_start_loop(actor)

        return actor

    def stop(self, address: str) -> None:
        """Stop an actor — set state to ended and cancel its loop task."""
        actor = self.actors.get(address)
        if actor is None:
            log.warning("stop: actor %s not found", address)
            return
        actor.state = "ended"
        self._cancel_task(address)

    def send(self, to: str, message: Message) -> None:
        """Deliver a message to an actor's mailbox. Drops silently if actor missing or ended."""
        actor = self.actors.get(to)
        if actor is None or actor.state == "ended":
            log.warning("send: dropping message to %s (not found or ended)", to)
            return
        mailbox = self.mailboxes.get(to)
        if mailbox is not None:
            mailbox.put_nowait(message)

    def lookup(self, address: str) -> Actor | None:
        """Look up an actor by address."""
        return self.actors.get(address)

    def attach(self, address: str, transport: Transport) -> None:
        """Attach a transport to an actor. Resumes a suspended actor."""
        actor = self.actors.get(address)
        if actor is None:
            log.warning("attach: actor %s not found", address)
            return
        actor.transport = transport
        if actor.state == "suspended":
            actor.state = "active"
            self._maybe_start_loop(actor)

    def detach(self, address: str) -> None:
        """Detach transport from an actor. Suspends the actor."""
        actor = self.actors.get(address)
        if actor is None:
            log.warning("detach: actor %s not found", address)
            return
        actor.transport = None
        actor.state = "suspended"
        self._cancel_task(address)

    def register_transport_handler(self, transport_type: str, callback: Callable) -> None:
        """Register a callback for a given transport type (e.g. 'websocket')."""
        self._transport_handlers[transport_type] = callback

    async def run(self) -> None:
        """Start all active actor loops and block until shutdown."""
        self._stop_event.clear()
        for actor in self.actors.values():
            if actor.state == "active":
                self._maybe_start_loop(actor)
        await self._stop_event.wait()

    async def shutdown(self) -> None:
        """Cancel all actor loop tasks and signal stop."""
        for address in list(self._tasks):
            self._cancel_task(address)
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Per-actor message loop
    # ------------------------------------------------------------------

    async def _actor_loop(self, actor: Actor) -> None:
        mailbox = self.mailboxes[actor.address]
        handler = get_handler(actor.handler)
        error_count = 0
        max_errors = 10
        try:
            while actor.state == "active":
                try:
                    msg = await asyncio.wait_for(mailbox.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                try:
                    log.info("Actor %s processing msg from %s type=%s", actor.address, msg.sender, msg.type)
                    actions = handler.handle(actor, msg)
                    log.info("Actor %s produced %d actions: %s", actor.address, len(actions), [type(a).__name__ for a in actions])
                    for action in actions:
                        self._execute(actor, action)
                    error_count = 0
                except Exception as e:
                    error_count += 1
                    log.error(
                        "actor %s handler error (%d/%d): %s",
                        actor.address,
                        error_count,
                        max_errors,
                        e,
                    )
                    if actor.parent:
                        self.send(
                            actor.parent,
                            Message(
                                sender=actor.address,
                                type="error",
                                payload={"error": str(e)},
                            ),
                        )
                    if error_count >= max_errors:
                        actor.state = "ended"
                        break
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Action execution
    # ------------------------------------------------------------------

    def _execute(self, actor: Actor, action: Action) -> None:
        if isinstance(action, Send):
            log.info("Execute Send: %s → %s", actor.address, action.to)
            self.send(action.to, action.message)
        elif isinstance(action, TransportSend):
            self._execute_transport_send(actor, action)
        elif isinstance(action, UpdateActor):
            self._execute_update(actor, action)
        elif isinstance(action, SpawnActor):
            self.spawn(action.address, action.handler, **action.kwargs)
        elif isinstance(action, StopActor):
            self.stop(action.address)

    def _execute_transport_send(self, actor: Actor, action: TransportSend) -> None:
        if actor.transport is None:
            log.warning("TransportSend on actor %s with no transport", actor.address)
            return
        callback = self._transport_handlers.get(actor.transport.type)
        if callback is None:
            log.warning(
                "No transport handler for type %s on actor %s",
                actor.transport.type,
                actor.address,
            )
            return
        result = callback(actor, action.payload)
        # If the callback is a coroutine, schedule it.
        if inspect.isawaitable(result):
            asyncio.ensure_future(result)

    def _execute_update(self, actor: Actor, action: UpdateActor) -> None:
        for key, value in action.changes.items():
            if key == "metadata":
                actor.metadata.update(value)
            else:
                setattr(actor, key, value)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _maybe_start_loop(self, actor: Actor) -> None:
        """Start an actor loop task if one isn't already running."""
        if actor.address in self._tasks and not self._tasks[actor.address].done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running event loop — loops will be started in run().
            return
        self._tasks[actor.address] = loop.create_task(self._actor_loop(actor))

    def _cancel_task(self, address: str) -> None:
        task = self._tasks.pop(address, None)
        if task is not None and not task.done():
            task.cancel()
