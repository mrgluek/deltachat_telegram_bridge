import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import dc_fingerprint

SELF_FP = "D58B5E6C55482F94F9F5B84B6FB9FE3AD2658082"
PEER_FP = "DEC0E8D11FC4686F2C715C56F8C9AD699BC19AD8"
ADMIN_FP = "9E1B0E5B8CB9CAE6A9D4F0D558C6D85CE9BD3041"
INVITE = f"https://i.delta.chat/#{SELF_FP}&v=3&i=abc&s=def&a=bot%40example.org&n=Bot"


def grouped(fp: str) -> str:
    """Fingerprint as the core prints it: groups of four, five groups per line."""
    groups = [fp[i:i + 4] for i in range(0, len(fp), 4)]
    return "\n".join(" ".join(groups[i:i + 5]) for i in range(0, len(groups), 5))


def encinfo(blocks, relays=("peer@example.org",)) -> str:
    text = "Messages are end-to-end encrypted.\nFingerprints:"
    for header, fp in blocks:
        text += f"\n\n{header}:\n{grouped(fp)}"
    if relays:
        text += "\n\nRelays:\n" + "\n".join(relays)
    return text


def make_rpc(info, invite=INVITE):
    rpc = MagicMock()
    rpc.get_contact_encryption_info.return_value = info
    rpc.get_chat_securejoin_qr_code.return_value = invite
    return rpc


class TestSelfFingerprint(unittest.TestCase):
    def test_current_invite_link(self):
        self.assertEqual(dc_fingerprint.get_self_fingerprint(make_rpc(""), 1), SELF_FP)

    def test_old_invite_link(self):
        rpc = make_rpc("", invite=f"OPENPGP4FPR:{SELF_FP.lower()}#a=bot%40example.org&n=Bot&i=a&s=b")
        self.assertEqual(dc_fingerprint.get_self_fingerprint(rpc, 1), SELF_FP)

    def test_rpc_error(self):
        rpc = make_rpc("")
        rpc.get_chat_securejoin_qr_code.side_effect = Exception("no account")
        self.assertIsNone(dc_fingerprint.get_self_fingerprint(rpc, 1))


class TestContactFingerprint(unittest.TestCase):
    def test_old_format_with_addresses(self):
        info = encinfo([("Me (bot@example.org)", SELF_FP), ("Peer (peer@example.org)", PEER_FP)])
        self.assertEqual(dc_fingerprint.get_contact_fingerprint(make_rpc(info), 1, 10), PEER_FP)

    def test_new_format_self_listed_first_or_last(self):
        for blocks in ([("Me", SELF_FP), ("Peer", PEER_FP)], [("Peer", PEER_FP), ("Me", SELF_FP)]):
            rpc = make_rpc(encinfo(blocks))
            self.assertEqual(dc_fingerprint.get_contact_fingerprint(rpc, 1, 10), PEER_FP)

    def test_translated_self_header(self):
        info = encinfo([("Ich", SELF_FP), ("Peer", PEER_FP)])
        self.assertEqual(dc_fingerprint.get_contact_fingerprint(make_rpc(info), 1, 10), PEER_FP)

    def test_name_cannot_inject_a_fingerprint(self):
        # The old parser stripped all separators and found ADMIN_FP inside the contact's name
        for name in (f"x{ADMIN_FP}x", f"x{ADMIN_FP}x (peer@example.org)", ADMIN_FP):
            rpc = make_rpc(encinfo([("Me", SELF_FP), (name, PEER_FP)]))
            self.assertEqual(dc_fingerprint.get_contact_fingerprint(rpc, 1, 10), PEER_FP)

    def test_contact_named_me(self):
        info = encinfo([("Me", PEER_FP), ("Me", SELF_FP)])
        self.assertEqual(dc_fingerprint.get_contact_fingerprint(make_rpc(info), 1, 10), PEER_FP)

    def test_v6_fingerprint(self):
        v6 = "AB" * 32
        info = encinfo([("Me", SELF_FP), ("Peer", v6)])
        self.assertEqual(dc_fingerprint.get_contact_fingerprint(make_rpc(info), 1, 10), v6)

    def test_no_key(self):
        rpc = make_rpc("No encryption.")
        self.assertIsNone(dc_fingerprint.get_contact_fingerprint(rpc, 1, 10))

    def test_relays_block_ignored(self):
        info = encinfo([("Me", SELF_FP), ("Peer", PEER_FP)], relays=("a@example.org", "b@example.org"))
        self.assertEqual(dc_fingerprint.parse_key_fingerprints(info), [SELF_FP, PEER_FP])

    def test_own_key_unknown_refuses(self):
        # Without our own fingerprint either block could be the contact's: never guess
        rpc = make_rpc(encinfo([("Me", SELF_FP), ("Peer", PEER_FP)]))
        rpc.get_chat_securejoin_qr_code.side_effect = Exception("rpc down")
        self.assertIsNone(dc_fingerprint.get_contact_fingerprint(rpc, 1, 10))

    def test_own_key_missing_from_text_refuses(self):
        rpc = make_rpc(encinfo([("Peer", PEER_FP), ("Other", ADMIN_FP)]))
        self.assertIsNone(dc_fingerprint.get_contact_fingerprint(rpc, 1, 10))

    def test_rpc_error(self):
        rpc = make_rpc("")
        rpc.get_contact_encryption_info.side_effect = Exception("special contact")
        self.assertIsNone(dc_fingerprint.get_contact_fingerprint(rpc, 1, 1))

    def test_self_fp_passed_in_skips_invite_lookup(self):
        rpc = make_rpc(encinfo([("Me", SELF_FP), ("Peer", PEER_FP)]))
        self.assertEqual(dc_fingerprint.get_contact_fingerprint(rpc, 1, 10, self_fp=SELF_FP), PEER_FP)
        rpc.get_chat_securejoin_qr_code.assert_not_called()


if __name__ == "__main__":
    unittest.main()
