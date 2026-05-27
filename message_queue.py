import asyncio
import logging
import os
import threading

logger = logging.getLogger("tg_dc_bridge")


class FileTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self._refs: dict[str, int] = {}

    def add_ref(self, path: str) -> None:
        if not path:
            return
        with self._lock:
            self._refs[path] = self._refs.get(path, 0) + 1

    def release(self, path: str) -> None:
        if not path:
            return
        with self._lock:
            remaining = self._refs.get(path, 0) - 1
            if remaining <= 0:
                self._refs.pop(path, None)
                try:
                    if os.path.exists(path):
                        os.unlink(path)
                except Exception:
                    pass
            else:
                self._refs[path] = remaining


class OrderedQueue:
    def __init__(self):
        self._queues: dict[str, asyncio.Queue] = {}
        self._workers: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def enqueue(self, key: str, coro):
        async with self._lock:
            queue = self._queues.get(key)
            if queue is None:
                queue = asyncio.Queue()
                self._queues[key] = queue
                self._workers[key] = asyncio.create_task(
                    self._worker(key, queue)
                )
        queue.put_nowait(coro)

    async def _worker(self, key: str, queue: asyncio.Queue):
        while True:
            coro = await queue.get()
            try:
                await coro
            except Exception as e:
                logger.error(f"Error in OrderedQueue worker [{key}]: {e}")
            finally:
                queue.task_done()


file_tracker = FileTracker()
ordered_queue = OrderedQueue()
