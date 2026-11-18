"""Tests for ensun VPN client rotate polling and timeout recovery."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from src.vpn_client import recent_rotate_from_status, rotate_vpn


class VPNClientTests(unittest.TestCase):
    def test_recent_rotate_from_status(self) -> None:
        status = {
            "last_rotate_age_seconds": 12.5,
            "last_result": {
                "ok": True,
                "old_ip": "1.1.1.1",
                "new_ip": "2.2.2.2",
            },
        }
        recovered = recent_rotate_from_status(status, max_age_seconds=180)
        self.assertIsNotNone(recovered)
        self.assertTrue(recovered.get("coalesced"))
        self.assertEqual(recovered.get("new_ip"), "2.2.2.2")

    def test_recent_rotate_rejects_stale(self) -> None:
        status = {
            "last_rotate_age_seconds": 999,
            "last_result": {"ok": True, "old_ip": "1.1.1.1", "new_ip": "2.2.2.2"},
        }
        self.assertIsNone(recent_rotate_from_status(status, max_age_seconds=180))

    def test_recent_rotate_uses_baseline_without_age(self) -> None:
        status = {
            "rotating": False,
            "last_rotate_age_seconds": None,
            "last_result": {
                "ok": True,
                "old_ip": "138.199.50.138",
                "new_ip": "185.132.133.172",
            },
        }
        recovered = recent_rotate_from_status(
            status,
            max_age_seconds=180,
            baseline_ip="138.199.50.138",
        )
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.get("new_ip"), "185.132.133.172")

    def test_rotate_vpn_coalesces_from_status_without_post(self) -> None:
        """If /status already shows a fresh IP change, never POST /rotate."""

        def fake_request(method, url, *, timeout=30, data=None):
            if method == "GET" and url.endswith("/status"):
                return 200, {
                    "rotating": False,
                    "last_rotate_age_seconds": 5,
                    "last_result": {
                        "ok": True,
                        "old_ip": "1.1.1.1",
                        "new_ip": "3.3.3.3",
                    },
                }
            if method == "POST" and url.endswith("/rotate"):
                return 500, {"ok": False, "error": "should not post"}
            return 404, {"ok": False}

        with patch("src.vpn_client._request", side_effect=fake_request):
            result = rotate_vpn(
                "http://127.0.0.1:8765",
                timeout=10,
                client_id="EUROPE/France",
                trigger="scraper_block",
                poll_seconds=0.1,
            )

        self.assertIsNotNone(result)
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("new_ip"), "3.3.3.3")
        self.assertTrue(result.get("coalesced"))

    def test_rotate_vpn_recovers_after_post_timeout_old_server(self) -> None:
        """Simulate 309s server rotate with 45s HTTP timeout (old app, no age field)."""
        calls = {"post": 0, "status": 0}

        def fake_request(method, url, *, timeout=30, data=None):
            if method == "GET" and url.endswith("/status"):
                calls["status"] += 1
                if calls["post"] == 0:
                    return 200, {
                        "rotating": False,
                        "last_result": {
                            "ok": True,
                            "old_ip": "138.199.50.138",
                            "new_ip": "138.199.50.138",
                        },
                    }
                if calls["status"] < 3:
                    return 200, {
                        "rotating": True,
                        "current_client": "EUROPE/France",
                        "last_result": {
                            "ok": True,
                            "old_ip": "138.199.50.138",
                            "new_ip": "138.199.50.138",
                        },
                    }
                return 200, {
                    "rotating": False,
                    "last_rotate_age_seconds": None,
                    "last_result": {
                        "ok": True,
                        "old_ip": "138.199.50.138",
                        "new_ip": "185.132.133.172",
                        "client_id": "EUROPE/France",
                    },
                }
            if method == "POST" and url.endswith("/rotate"):
                calls["post"] += 1
                return 0, {"ok": False, "error": "timed out"}
            return 404, {"ok": False}

        with patch("src.vpn_client._request", side_effect=fake_request):
            result = rotate_vpn(
                "http://127.0.0.1:8765",
                timeout=10,
                client_id="EUROPE/France",
                trigger="scraper_block",
                poll_seconds=0.05,
                request_timeout=1.0,
            )

        self.assertIsNotNone(result)
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("new_ip"), "185.132.133.172")
        self.assertEqual(calls["post"], 1)


if __name__ == "__main__":
    unittest.main()
