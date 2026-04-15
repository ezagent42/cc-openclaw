"""Persistence helpers — save/load actors to/from a JSON file with atomic write."""
from __future__ import annotations

import json
from pathlib import Path

from channel_server.core.actor import Actor


def save_actors(actors: dict[str, Actor], filepath: Path) -> None:
    """Save active/suspended actors to JSON. Ended actors are not persisted.

    Uses an atomic write: data is written to a .tmp file then renamed into
    place so readers never see a partial file.
    """
    data = {}
    for address, actor in actors.items():
        if actor.state != "ended":
            data[address] = actor.to_dict()
    tmp = filepath.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.rename(filepath)


def load_actors(filepath: Path) -> dict[str, Actor]:
    """Load actors from JSON. Returns an empty dict if the file is missing or corrupt."""
    if not filepath.exists():
        return {}
    try:
        data = json.loads(filepath.read_text())
        return {addr: Actor.from_dict(d) for addr, d in data.items()}
    except Exception:
        return {}
