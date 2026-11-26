# SPDX-License-Identifier: GPL-3.0-or-later
"""Run slow queries (zypper, rpm, the network) in a thread so the window never freezes.

Daemon threads: closing the window ends the program at once, even if a slow network question is still
waiting for an answer (its result is simply dropped). Long writes are made safe where they happen:
downloads go to a '.part' file that is cleaned at the next start.

One dispatcher object, made in the GUI thread, carries every result back to it: nothing is created per
call, so nothing piles up during a long session.
"""

import threading

from PySide6.QtCore import QCoreApplication, QObject, Signal

_lock = threading.Lock()
_closing = False
_dispatcher = None


class _Dispatcher(QObject):
    done = Signal(object, object, object)  # callback, result, exception

    def __init__(self):
        super().__init__()
        self.done.connect(self._deliver)

    @staticmethod
    def _deliver(callback, result, error):
        if not _closing:
            callback(result, error)


def _get_dispatcher():
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = _Dispatcher()  # the first call comes from the GUI thread, so it lives there
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(shutting_down)
    return _dispatcher


def shutting_down():
    """From now on no thread hands anything back: the window and its objects are going away."""
    global _closing
    with _lock:
        _closing = True


def closing():
    """True once the program is quitting: threads of their own (repocheck) must not emit any more."""
    return _closing


def emit_safely(signal, *args):
    """Emit a Qt signal from a worker thread unless the program is quitting (it would crash in teardown)."""
    with _lock:
        if not _closing:
            signal.emit(*args)


def run_async(fn, *args, on_done, **kwargs):
    """Call fn(*args, **kwargs) in a thread; on_done(result, error) runs in the GUI thread."""
    disp = _get_dispatcher()

    def work():
        try:
            result, error = fn(*args, **kwargs), None
        except Exception as exc:  # reported to the caller, never raised in the thread
            result, error = None, exc
        with _lock:  # never emit while Qt and Python are tearing down
            if not _closing:
                disp.done.emit(on_done, result, error)

    threading.Thread(target=work, daemon=True, name=getattr(fn, "__name__", "job")).start()


def collect_in_gui_thread(app):
    """Run Python's cycle collector only in the GUI thread, from a timer.

    Left to itself, the collector runs in whichever thread happens to allocate. A run_async worker could then
    finalise Qt wrappers that the GUI thread was destroying at that very moment (rebuilding the rows of the queue
    review, cards, the routine band): a random segfault (code review 0.5, #1: core dump of 26/11, reproduced in
    1 of 4 runs of 90 s; none in 6 runs with this). Plain reference counting still frees everything else at once."""
    import gc

    from PySide6.QtCore import QTimer
    gc.disable()
    timer = QTimer(app)
    timer.setInterval(1000)
    ticks = [0]

    def tick():
        ticks[0] += 1
        if ticks[0] % 60 == 0:
            gc.collect()
        elif gc.get_count()[0] > 700:  # the collector's own default threshold for the youngest generation
            gc.collect(0)

    timer.timeout.connect(tick)
    timer.start()
    app._zypp_desk_gc_timer = timer
    return timer
