import asyncio
import time
import unittest
from unittest.mock import patch

import bot  # noqa: F401  (sets up the module graph the same way as the bot)
import security


class TestDcSendLimiter(unittest.TestCase):
    def setUp(self):
        security._dc_send_tokens = 0.0
        security._dc_send_tokens_ts = time.monotonic()
        security._dc_send_dispatcher = None
        for q in security._dc_send_waiters.values():
            q.clear()

    def test_groups_go_before_channels_and_order_is_kept(self):
        order = []

        async def send(tag, prio):
            await security._wait_for_global_dc_rate_limit(prio)
            order.append(tag)

        async def scenario():
            # Fast refill so the test runs quickly; the bucket starts empty.
            with patch.object(security, "GLOBAL_DC_RATE_LIMIT", 600), patch.object(security, "_notify_owner_about_backlog", lambda: None):
                tasks = [asyncio.create_task(send(f"c{i}", security.PRIORITY_CHANNEL)) for i in range(3)]
                await asyncio.sleep(0)
                tasks += [asyncio.create_task(send(f"g{i}", security.PRIORITY_GROUP)) for i in range(2)]
                await asyncio.gather(*tasks)

        asyncio.run(scenario())
        self.assertEqual(order, ["g0", "g1", "c0", "c1", "c2"])

    def test_burst_is_capped(self):
        security._dc_send_tokens = float(security.DC_SEND_BURST)
        sent_at = []

        async def send():
            await security._wait_for_global_dc_rate_limit(security.PRIORITY_CHANNEL)
            sent_at.append(time.monotonic())

        async def scenario():
            with patch.object(security, "_notify_owner_about_backlog", lambda: None):
                start = time.monotonic()
                await asyncio.gather(*(send() for _ in range(security.DC_SEND_BURST + 1)))
                return start

        start = asyncio.run(scenario())
        immediate = [t for t in sent_at if t - start < 0.5]
        self.assertEqual(len(immediate), security.DC_SEND_BURST)
        # The one after the burst waits for a refill (60/50 = 1.2 s per token).
        self.assertGreaterEqual(max(sent_at) - start, 1.0)

    def test_cancelled_waiter_is_skipped(self):
        got = []

        async def scenario():
            with patch.object(security, "GLOBAL_DC_RATE_LIMIT", 600), patch.object(security, "_notify_owner_about_backlog", lambda: None):
                t1 = asyncio.create_task(security._wait_for_global_dc_rate_limit())
                t2 = asyncio.create_task(security._wait_for_global_dc_rate_limit())
                await asyncio.sleep(0)
                t1.cancel()
                await t2
                got.append(t1.cancelled())

        asyncio.run(scenario())
        self.assertEqual(got, [True])


if __name__ == "__main__":
    unittest.main()
