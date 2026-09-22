"""Thread-safety wrapper for the JSON-RPC connection, and the shared
semaphore that throttles concurrent Telethon media downloads.

Pure leaf module.
"""
import asyncio
import threading

_download_semaphore = None


class RpcProxy:
    """Thread-safe proxy for Rpc to prevent race conditions on JSON-RPC stdin/stdout pipes."""
    def __init__(self, rpc_instance):
        self._rpc = rpc_instance
        self._lock = threading.Lock()

    def __getattr__(self, name):
        attr = getattr(self._rpc, name)
        if callable(attr):
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
