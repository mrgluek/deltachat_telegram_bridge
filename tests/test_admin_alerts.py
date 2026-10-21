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


class TestAdminDcDelivery(unittest.TestCase):
    """Admin log messages must go to the admin's key contact: the address-only one cannot be written to."""
    FP = "DFF2CAB1FEB7182F997C0A01466AA64DE33D8A39"

    def setUp(self):
        from unittest.mock import MagicMock, patch
        import dc_helpers
        self.dc_helpers = dc_helpers
        cfg = {"admin_dc_email": "admin@example.org", "admin_dc_fingerprint": self.FP}
        self.patches = [
            patch("database.get_config", side_effect=lambda k, *a: cfg.get(k)),
            patch("database.get_admin_fingerprint", side_effect=lambda: cfg["admin_dc_fingerprint"]),
            patch("dc_helpers._get_contact_fingerprint",
                  side_effect=lambda b, a, cid, c=None: {11: "AAAA" * 10, 12: self.FP}.get(cid)),
        ]
        for p in self.patches:
            p.start()
        self.cfg = cfg
        contacts = {
            10: {"address": "admin@example.org", "is_key_contact": False, "last_seen": 0},
            11: {"address": "admin@example.org", "is_key_contact": True, "last_seen": 900},
            12: {"address": "admin@example.org", "is_key_contact": True, "last_seen": 100},
        }
        self.dc = MagicMock()
        self.dc.rpc.get_contact_ids.return_value = [10, 11, 12]
        self.dc.rpc.get_contact.side_effect = lambda acc, cid: contacts[cid]
        self.dc.rpc.lookup_contact_id_by_addr.return_value = 10
        self.dc.rpc.create_chat_by_contact_id.side_effect = lambda acc, cid: 1000 + cid
        logging_setup._admin_dc_chat_id_cache = None
        logging_setup._admin_cached_at = 0.0

    def tearDown(self):
        for p in self.patches:
            p.stop()
        logging_setup._admin_dc_chat_id_cache = None
        logging_setup._admin_cached_at = 0.0

    def test_contact_with_admin_fingerprint(self):
        self.assertEqual(self.dc_helpers.admin_key_contact_id(self.dc, 1), 12)

    def test_without_fingerprint_latest_key_contact(self):
        self.cfg["admin_dc_fingerprint"] = ""
        self.assertEqual(self.dc_helpers.admin_key_contact_id(self.dc, 1), 11)

    def test_log_message_sent_to_key_chat_and_cached(self):
        from unittest.mock import patch
        with patch.object(bot, "dc_bot_instance", self.dc), patch.object(bot, "dc_accid", 1):
            logging_setup._send_admin_dc_message_bg("boom")
            logging_setup._send_admin_dc_message_bg("boom again")
        self.assertEqual([c.args[1] for c in self.dc.rpc.send_msg.call_args_list], [1012, 1012])
        self.assertEqual(self.dc.rpc.create_chat_by_contact_id.call_count, 1)  # the chat is cached
        self.assertFalse(self.dc.rpc.create_contact.called)                   # no address-only contact


if __name__ == "__main__":
    unittest.main()
