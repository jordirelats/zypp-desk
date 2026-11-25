# SPDX-License-Identifier: GPL-3.0-or-later
"""Run slow queries (zypper, rpm, the network) in a thread so the window never freezes.

Daemon threads: closing the window ends the program at once, even if a slow network question is still
waiting for an answer (its result is simply dropped). Long writes are made safe where they happen:
downloads go to a '.part' file that is cleaned at the next start.
"""

import threading

from PySide6.QtCore import QObject, Signal

_alive = set()


class _Signals(QObject):
    done = Signal(object, object)  # result, exception


def run_async(fn, *args, on_done, **kwargs):
    """Call fn(*args, **kwargs) in a thread; on_done(result, error) runs in the GUI thread.

    The signals object is created here, in the GUI thread, so the queued call lands back in it.
    """
    signals = _Signals()
    _alive.add(signals)

    def finish(result, error):
        _alive.discard(signals)
        on_done(result, error)

    def work():
        try:
            result, error = fn(*args, **kwargs), None
        except Exception as exc:  # reported to the caller, never raised in the thread
            result, error = None, exc
        try:
            signals.done.emit(result, error)
        except RuntimeError:
            pass  # the window was closed while the query was still running

    signals.done.connect(finish)
    threading.Thread(target=work, daemon=True, name=getattr(fn, "__name__", "job")).start()
