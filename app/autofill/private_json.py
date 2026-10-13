"""
Owner-only files under ``~/.config/firstplay``.

The profile is written 0600 (``memory.save_memory``) because it holds
protected characteristics and contact details. Every file *derived* from it
used to undo that: ``labels.json`` keyed rows on the value, ``options.json``
once held ``label::stored value::options`` keys, and the coverage and audit
dumps carry whole plans — all written with the default umask, 0644, readable
by any account on the machine or any backup of ``~/.config`` (audit
2026-10-12, F5).

One helper, so no tool has to remember: the file is created 0600 from the
first byte (``os.open`` with the mode, not a ``chmod`` after a world-readable
window), written to a sibling temporary and moved into place, so an
interrupted write never leaves a half-serialised file behind.
"""
import json
import os
from contextlib import contextmanager
from typing import Any, Iterator, TextIO

OWNER_ONLY = 0o600


@contextmanager
def private_file(path: str) -> Iterator[TextIO]:
    """Open ``path`` for writing as an owner-only file, atomically replaced.

    Yields a text handle onto a temporary sibling; on a clean exit the
    temporary is moved over ``path`` and the final mode is forced to 0600
    even when a world-readable file was already there.
    """
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    temporary = path + ".tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, OWNER_ONLY)
    handle = os.fdopen(descriptor, "w")
    try:
        yield handle
        handle.flush()
    except BaseException:
        handle.close()
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    handle.close()
    os.replace(temporary, path)
    os.chmod(path, OWNER_ONLY)


def write_private_json(path: str, data: Any, **dump_kwargs: Any) -> str:
    """``json.dump(data)`` into an owner-only file at ``path``. Returns ``path``."""
    with private_file(path) as handle:
        json.dump(data, handle, **dump_kwargs)
    return path
