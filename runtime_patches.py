"""Process-wide monkeypatches applied once at bot startup.

These patch third-party library internals (Telethon) and the interpreter's
unraisable-exception hook, so they must run exactly once, as early as
possible, regardless of which module happens to be imported first.
"""
import asyncio
import logging
import sys

logger = logging.getLogger("tg_dc_bridge")

_applied = False

try:
    from telethon.network.mtprotosender import MTProtoSender
    from telethon import helpers
    from telethon.helpers import retry_range
    from telethon.errors import InvalidBufferError, AuthKeyNotFound

    async def _safe_telethon_reconnect(self, last_error):
        # If connection is None, this sender was already disconnected or abandoned.
        # Reconnecting to None is impossible and causes infinite AttributeError loops.
        if getattr(self, '_connection', None) is None:
            self._log.info('Cannot reconnect MTProtoSender: _connection is None.')
            return

        self._log.info('Closing current connection to begin reconnect...')
        try:
            if self._connection:
                await self._connection.disconnect()
        except Exception:
            pass

        await helpers._cancel(
            self._log,
            send_loop_handle=self._send_loop_handle,
            recv_loop_handle=self._recv_loop_handle
        )

        self._reconnecting = False
        self._state.reset()

        retries = self._retries if self._auto_reconnect else 0

        attempt = 0
        ok = True
        for attempt in retry_range(retries, force_retry=False):
            if getattr(self, '_connection', None) is None:
                self._log.info('MTProtoSender connection is None; aborting reconnect.')
                ok = False
                break
            try:
                await self._connect()
            except (IOError, asyncio.TimeoutError) as e:
                last_error = e
                self._log.info('Failed reconnection attempt %d with %s',
                               attempt, e.__class__.__name__)
                await asyncio.sleep(self._delay)
            except BufferError as e:
                if isinstance(e, InvalidBufferError) and e.code == 404:
                    self._log.info('Server does not know about the current auth key; the session may need to be recreated')
                    last_error = AuthKeyNotFound()
                    ok = False
                    break
                else:
                    self._log.warning('Invalid buffer %s', e)
            except Exception as e:
                if getattr(self, '_connection', None) is None:
                    self._log.info('MTProtoSender connection became None during reconnect; aborting.')
                    ok = False
                    break
                last_error = e
                self._log.exception('Unexpected exception reconnecting on attempt %d', attempt)
                await asyncio.sleep(self._delay)
            else:
                self._send_queue.extend(self._pending_state.values())
                self._pending_state.clear()

                if self._auto_reconnect_callback:
                    helpers.get_running_loop().create_task(self._auto_reconnect_callback())
                break
        else:
            ok = False

        if not ok:
            self._log.error('Automatic reconnection failed %d time(s)', attempt)
            error = last_error.with_traceback(None) if last_error else None
            await self._disconnect(error=error)
except Exception:
    MTProtoSender = None
    _safe_telethon_reconnect = None


def _patch_telethon_reconnect():
    try:
        if MTProtoSender is None or _safe_telethon_reconnect is None:
            raise RuntimeError("Telethon MTProtoSender unavailable")
        MTProtoSender._reconnect = _safe_telethon_reconnect
    except Exception as _patch_err:
        logger.warning(f"Could not patch Telethon MTProtoSender._reconnect: {_patch_err}")


def _custom_unraisablehook(unraisable):
    """Suppress benign Telethon GeneratorExit cleanup noise during garbage collection."""
    if unraisable.exc_type is RuntimeError and "coroutine ignored GeneratorExit" in str(unraisable.exc_value):
        obj_name = getattr(unraisable.object, '__qualname__', '') or getattr(unraisable.object, '__name__', '') or str(unraisable.object)
        if any(k in obj_name for k in ("_recv_loop", "_send_loop", "Connection", "MTProtoSender", "coroutine")):
            logger.debug(f"Suppressed benign Telethon finalizer GeneratorExit in {obj_name}")
            return
    if hasattr(sys, '__unraisablehook__') and sys.__unraisablehook__:
        sys.__unraisablehook__(unraisable)


def apply():
    global _applied
    if _applied:
        return
    _applied = True
    _patch_telethon_reconnect()
    sys.unraisablehook = _custom_unraisablehook
