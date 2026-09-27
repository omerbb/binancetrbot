"""One bounded inference in flight; workers never access trading state or SQLite.

Python cannot kill a blocked HTTP thread. Cancellation discards its result, prevents
retries, and refuses overlapping work until it exits. Provider cleanup is deferred
until that exit, so closing a controller never waits for network I/O.
"""
import copy
import queue
import threading

from decision.openrouter import ProviderError


class InferenceTask:
    def __init__(self):
        self.done = threading.Event()
        self.cancelled = threading.Event()
        self.attempts = queue.SimpleQueue()
        self.response = None
        self.error = None


class InferenceRunner:
    def __init__(self, provider):
        self.provider = provider
        self.lock = threading.Lock()
        self.task = None
        self.closed = False
        self.provider_closed = False

    @property
    def busy(self):
        with self.lock:
            return self.task is not None and not self.task.done.is_set()

    def submit(self, state, questions, deadline):
        with self.lock:
            if self.closed:
                raise ProviderError("inference_runner_closed")
            if self.task is not None and not self.task.done.is_set():
                raise ProviderError("inference_still_running")
            task = self.task = InferenceTask()
            state, questions = copy.deepcopy(state), copy.deepcopy(questions)

            def run():
                try:
                    method = getattr(self.provider, "evaluate_with_deadline", None)
                    if method is not None:
                        task.response = method(state, questions, deadline=deadline,
                                               cancelled=task.cancelled.is_set,
                                               on_attempt=task.attempts.put)
                    else:
                        task.response = self.provider.evaluate(state, questions, on_attempt=task.attempts.put)
                except Exception as exc:
                    task.error = exc
                finally:
                    with self.lock:
                        task.done.set()
                        if self.closed:
                            self._close_provider()

            threading.Thread(target=run, name="jev-inference", daemon=True).start()
            return task

    def cancel(self):
        with self.lock:
            if self.task is not None:
                self.task.cancelled.set()

    def _close_provider(self):
        if not self.provider_closed:
            self.provider_closed = True
            if hasattr(self.provider, "close"):
                self.provider.close()

    def close(self):
        with self.lock:
            self.closed = True
            if self.task is not None:
                self.task.cancelled.set()
            if self.task is None or self.task.done.is_set():
                self._close_provider()
