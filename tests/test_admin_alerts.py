import logging
import os
import sys
import unittest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: F401  (ensures module import order matches production)
import logging_setup
from logging_setup import _admin_alert_check, _admin_alert_key, ADMIN_ALERT_THROTTLE_SECONDS


def _record(msg, exc=None):
    return logging.LogRecord(
        "tg_dc_bridge", logging.ERROR, __file__, 1, msg, None,
        (type(exc), exc, None) if exc else None,
    )


class TestAdminAlertThrottle(unittest.TestCase):
    def setUp(self):
        logging_setup._admin_alert_last_sent.clear()
        logging_setup._admin_alert_suppressed.clear()

    def test_key_ignores_numbers(self):
        a = _admin_alert_key(_record("Post @artjockey/3424: 4 of 4 images failed"))
        b = _admin_alert_key(_record("Post @artjockey/3402: 6 of 6 images failed"))
        self.assertEqual(a, b)

    def test_key_distinguishes_exception_types(self):
        a = _admin_alert_key(_record("Failed to extract", NameError("x")))
        b = _admin_alert_key(_record("Failed to extract", TypeError("x")))
        self.assertNotEqual(a, b)

    def test_throttles_within_window_and_reports_suppressed(self):
        key = "k"
        self.assertEqual(_admin_alert_check(key, now=1000.0), (True, 0))
        self.assertEqual(_admin_alert_check(key, now=1001.0), (False, 0))
        self.assertEqual(_admin_alert_check(key, now=1002.0), (False, 0))
        later = 1000.0 + ADMIN_ALERT_THROTTLE_SECONDS
        self.assertEqual(_admin_alert_check(key, now=later), (True, 2))
        self.assertEqual(_admin_alert_check(key, now=later + 1), (False, 0))

    def test_different_keys_independent(self):
        self.assertTrue(_admin_alert_check("a", now=1000.0)[0])
        self.assertTrue(_admin_alert_check("b", now=1000.0)[0])


if __name__ == "__main__":
    unittest.main()
