"""Thread-safety wrapper for the JSON-RPC connection, and the shared
semaphore that throttles concurrent Telethon media downloads.

Pure leaf module.
"""
import asyncio
import threading

_download_semaphore = None


# Long-polling RPC methods: they return only when the core has something new,
# which can take minutes. They must not hold the lock, or every other RPC call
# (e.g. send_msg from the asyncio loop) waits for the next core event and the
# whole bridge freezes meanwhile. deltachat2's IOTransport is safe to call from
# several threads (one writer thread, results matched by request id), so
# running them unlocked is fine.
_UNLOCKED_METHODS = frozenset({"get_next_event", "wait_next_msgs"})


class RpcProxy:
    """Thread-safe proxy for Rpc to prevent race conditions on JSON-RPC stdin/stdout pipes."""
    def __init__(self, rpc_instance):
        self._rpc = rpc_instance
        self._lock = threading.Lock()

    def __getattr__(self, name):
        attr = getattr(self._rpc, name)
        if callable(attr) and name not in _UNLOCKED_METHODS:
            def wrapped(*args, **kwargs):
                with self._lock:
                    return attr(*args, **kwargs)
            return wrapped
        return attr


def _make_rpc_thread_safe(bot):
    if hasattr(bot, 'rpc') and not isinstance(bot.rpc, RpcProxy):
        bot.rpc = RpcProxy(bot.rpc)


def _get_download_semaphore():
    global _download_semaphore
    if _download_semaphore is None:
        _download_semaphore = asyncio.Semaphore(3)
    return _download_semaphore
