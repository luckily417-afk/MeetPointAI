"""把 asyncio 事件循环放进后台线程，GUI 主线程永不阻塞。

* 所有协程通过 `submit()` 投递，结果以 Qt 信号回到主线程；
* 支持任务取消（用户修改 A/B 点或区域时，旧任务立即失效）；
* 进度回调通过 `progress` 信号排队到主线程，避免跨线程直接操作控件。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import threading
import traceback
import uuid
from typing import Any, Awaitable, Callable

from PySide6.QtCore import QObject, Qt, Signal

__all__ = ["AsyncRuntime"]


class AsyncRuntime(QObject):
    done = Signal(str, object)
    failed = Signal(str, str)
    progress = Signal(str, int, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="mpai-asyncio", daemon=True)
        self._thread.start()
        self._callbacks: dict[str, tuple[Callable[[Any], None] | None, Callable[[str], None] | None]] = {}
        self._futures: dict[str, concurrent.futures.Future[Any]] = {}
        self._lock = threading.Lock()
        self.done.connect(self._dispatch_done, Qt.QueuedConnection)
        self.failed.connect(self._dispatch_failed, Qt.QueuedConnection)

    # ------------------------------------------------------------------ #
    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            try:
                # 必须显式传入 loop：run_forever 已返回，此时没有 running loop，
                # asyncio.all_tasks() 会抛 RuntimeError，挂起的任务就永远取消不掉。
                pending = asyncio.all_tasks(self._loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            except Exception:  # noqa: BLE001
                pass
            self._loop.close()

    # ------------------------------------------------------------------ #
    def submit(
        self,
        coro: Awaitable[Any],
        *,
        on_done: Callable[[Any], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        task_id: str | None = None,
    ) -> str:
        tid = task_id or uuid.uuid4().hex
        with self._lock:
            self._callbacks[tid] = (on_done, on_error)
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)  # type: ignore[arg-type]
        with self._lock:
            self._futures[tid] = future
        future.add_done_callback(lambda f, t=tid: self._settle(t, f))
        return tid

    def _settle(self, task_id: str, future: concurrent.futures.Future[Any]) -> None:
        try:
            result = future.result()
        except concurrent.futures.CancelledError:
            self.failed.emit(task_id, "__cancelled__")
            return
        except BaseException as exc:  # noqa: BLE001
            message = str(exc) or exc.__class__.__name__
            if not isinstance(exc, (asyncio.CancelledError,)):
                detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
                message = detail or message
            self.failed.emit(task_id, message)
            return
        self.done.emit(task_id, result)

    def _dispatch_done(self, task_id: str, result: Any) -> None:
        with self._lock:
            callbacks = self._callbacks.pop(task_id, (None, None))
            self._futures.pop(task_id, None)
        on_done, _ = callbacks
        if on_done:
            try:
                on_done(result)
            except Exception:  # noqa: BLE001 - UI 回调异常不应杀进程
                traceback.print_exc()

    def _dispatch_failed(self, task_id: str, message: str) -> None:
        with self._lock:
            callbacks = self._callbacks.pop(task_id, (None, None))
            self._futures.pop(task_id, None)
        _, on_error = callbacks
        if on_error:
            try:
                on_error(message)
            except Exception:  # noqa: BLE001
                traceback.print_exc()

    # ------------------------------------------------------------------ #
    def cancel(self, task_id: str) -> None:
        with self._lock:
            future = self._futures.get(task_id)
        if future is not None:
            future.cancel()

    def make_progress(self, task_id: str) -> Callable[[int, str], None]:
        """生成可在协程内安全调用的进度回调。"""

        def callback(percent: int, message: str) -> None:
            self.progress.emit(task_id, int(percent), str(message))

        return callback

    # ------------------------------------------------------------------ #
    def run_sync(self, coro: Awaitable[Any], timeout: float = 30.0) -> Any:
        """测试/脚本用：同步等待协程结果（不要在 GUI 主线程调用）。"""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)  # type: ignore[arg-type]
        return future.result(timeout=timeout)

    def is_running(self) -> bool:
        return self._thread.is_alive()

    def stop(self) -> None:
        if not self._thread.is_alive():
            return
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except RuntimeError:
            pass
        self._thread.join(timeout=5.0)
