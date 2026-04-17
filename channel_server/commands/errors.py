"""Exceptions raised by the command registry."""
from __future__ import annotations


class CommandError(Exception):
    """Base class for command-layer errors. Surfaces to the user as 'command failed'."""


class UnknownCommand(CommandError):
    """Raised by a scope when no matching command is found in the chain."""
    def __init__(self, name: str):
        self.name = name
        super().__init__(f"unknown command: /{name}")


class BadArgs(CommandError):
    """Raised by bind_args when tokens cannot be bound to the command's schema."""
