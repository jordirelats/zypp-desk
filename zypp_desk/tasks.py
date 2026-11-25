# SPDX-License-Identifier: GPL-3.0-or-later
"""Run slow read-only queries in a thread so the window never freezes."""

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

_alive = set()


class _Signals(QObject):
    done = Signal(object, object)  # result, exception


class _Job(QRunnable):
    def __init__(self, fn, args, kwargs, signals):
        super().__init__()
        self.fn, self.args, self.kwargs, self.signals = fn, args, kwargs, signals

    def run(self):
        try:
            result = self.fn(*self.args, **self.kwargs)
        except Exception as exc:  # reported to the caller, never raised in the thread
            self.signals.done.emit(None, exc)
        else:
            self.signals.done.emit(result, None)


def run_async(fn, *args, on_done, **kwargs):
    """Call fn(*args, **kwargs) in the thread pool; on_done(result, error) runs in the GUI thread.

    The signals object is created here, in the GUI thread, so the queued call lands back in it.
    """
    signals = _Signals()
    _alive.add(signals)

    def finish(result, error):
        _alive.discard(signals)
        on_done(result, error)

    signals.done.connect(finish)
    job = _Job(fn, args, kwargs, signals)
    QThreadPool.globalInstance().start(job)
