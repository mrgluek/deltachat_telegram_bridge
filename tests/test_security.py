import asyncio
import io
import os
import socket
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import bot


class TestSecurity(unittest.TestCase):
    def test_safe_telegram_urls(self):
        # Valid telegram domains
        self.assertTrue(bot._is_safe_telegram_url("https://t.me/durov/123"))
        self.assertTrue(bot._is_safe_telegram_url("https://telegram.org/img/logo.png"))
        self.assertTrue(bot._is_safe_telegram_url("https://cdn4.telesco.pe/file/avatar.jpg"))
        self.assertTrue(bot._is_safe_telegram_url("https://stel.com/resource.jpg"))

    def test_rejected_schemes(self):
        self.assertFalse(bot._is_safe_telegram_url("ftp://t.me/durov"))
        self.assertFalse(bot._is_safe_telegram_url("file:///etc/passwd"))
        self.assertFalse(bot._is_safe_telegram_url("javascript:alert(1)"))
        self.assertFalse(bot._is_safe_telegram_url(""))
        self.assertFalse(bot._is_safe_telegram_url(None))

    def test_rejected_untrusted_hosts(self):
        self.assertFalse(bot._is_safe_telegram_url("https://attacker.com/evil.jpg"))
        self.assertFalse(bot._is_safe_telegram_url("https://evil-t.me/photo.jpg"))
        self.assertFalse(bot._is_safe_telegram_url("https://fake.telegram.org.attacker.com/test"))

    def test_rejected_local_and_private_hostnames(self):
        self.assertFalse(bot._is_safe_telegram_url("http://localhost/image.jpg"))
        self.assertFalse(bot._is_safe_telegram_url("http://service.local/image.jpg"))
        self.assertFalse(bot._is_safe_telegram_url("http://internal.lan/image.jpg"))
        self.assertFalse(bot._is_safe_telegram_url("http://db.internal/image.jpg"))

    def test_rejected_ip_literals(self):
        self.assertFalse(bot._is_safe_telegram_url("http://127.0.0.1/test.png"))
        self.assertFalse(bot._is_safe_telegram_url("http://169.254.169.254/latest/meta-data"))
        self.assertFalse(bot._is_safe_telegram_url("http://192.168.1.1/test.png"))
        self.assertFalse(bot._is_safe_telegram_url("http://10.0.0.1/test.png"))
        self.assertFalse(bot._is_safe_telegram_url("http://[::1]/test.png"))

    def test_dns_resolution_private_ip_blocked(self):
        # Even if domain ends with t.me, if DNS resolves to loopback/private, block it
        fake_addr_info = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443)),
        ]
        with patch('socket.getaddrinfo', return_value=fake_addr_info):
            self.assertFalse(bot._is_safe_telegram_url("https://t.me/test"))

        fake_private_info = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.0.5', 443)),
        ]
        with patch('socket.getaddrinfo', return_value=fake_private_info):
            self.assertFalse(bot._is_safe_telegram_url("https://t.me/test"))

        fake_metadata_info = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('169.254.169.254', 443)),
        ]
        with patch('socket.getaddrinfo', return_value=fake_metadata_info):
            self.assertFalse(bot._is_safe_telegram_url("https://t.me/test"))

        fake_ipv6_mapped = [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('::ffff:127.0.0.1', 443)),
        ]
        with patch('socket.getaddrinfo', return_value=fake_ipv6_mapped):
            self.assertFalse(bot._is_safe_telegram_url("https://t.me/test"))

    def test_dns_resolution_public_ip_allowed(self):
        fake_public_info = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('149.154.167.99', 443)),
        ]
        with patch('socket.getaddrinfo', return_value=fake_public_info):
            self.assertTrue(bot._is_safe_telegram_url("https://t.me/test"))

    def test_extract_public_tg_post_rich_input_validation(self):
        # Invalid usernames
        self.assertIsNone(asyncio.run(bot._extract_public_tg_post_rich("../traversal", 100)))
        self.assertIsNone(asyncio.run(bot._extract_public_tg_post_rich("user/sub", 100)))
        self.assertIsNone(asyncio.run(bot._extract_public_tg_post_rich("bad-user!name", 100)))
        self.assertIsNone(asyncio.run(bot._extract_public_tg_post_rich("", 100)))

        # Invalid post IDs
        self.assertIsNone(asyncio.run(bot._extract_public_tg_post_rich("durov", 0)))
        self.assertIsNone(asyncio.run(bot._extract_public_tg_post_rich("durov", -5)))
        self.assertIsNone(asyncio.run(bot._extract_public_tg_post_rich("durov", "not-an-int")))

    def test_download_image_size_limit(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b'X' * (21 * 1024 * 1024)  # 21 MB (exceeds 20MB limit)

        with patch('bot._is_safe_telegram_url', return_value=True), \
             patch('httpx.AsyncClient.get', new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp

            tmp_fd, tmp_file = tempfile.mkstemp(suffix=".jpg")
            os.close(tmp_fd)
            try:
                ok = asyncio.run(bot._download_image_to_file("https://t.me/fake.jpg", tmp_file))
                self.assertFalse(ok)
            finally:
                if os.path.exists(tmp_file):
                    os.unlink(tmp_file)

            path = asyncio.run(bot._download_image_url("https://t.me/fake.jpg"))
            self.assertIsNone(path)


if __name__ == "__main__":
    unittest.main()
