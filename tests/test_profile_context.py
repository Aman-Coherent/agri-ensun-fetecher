"""Profile browser session reuse helpers."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from src.session import BrowserSession


class TestProfileContextHelpers(unittest.TestCase):
    @patch("src.session.settings")
    def test_note_profile_fetch_increments_counter(self, mock_settings) -> None:
        mock_settings.return_value = {"profile_context_reset_every": 50}
        session = BrowserSession()
        self.assertEqual(session._profile_fetch_count, 0)
        session.note_profile_fetch_complete()
        self.assertEqual(session._profile_fetch_count, 1)

    @patch("src.session.settings")
    def test_profile_settings_defaults(self, mock_settings) -> None:
        mock_settings.return_value = {}
        session = BrowserSession()
        self.assertEqual(session._profile_page_ready_poll_ms(), 400)
        self.assertEqual(session._profile_tab_settle_ms(), 800)
        self.assertEqual(session._profile_context_reset_every(), 50)
        self.assertEqual(session._profile_goto_wait_until(), "domcontentloaded")

    @patch("src.session.settings")
    def test_reset_every_zero_disables_periodic_reset(self, mock_settings) -> None:
        mock_settings.return_value = {"profile_context_reset_every": 0}
        session = BrowserSession()
        session._profile_fetch_count = 100
        self.assertEqual(session._profile_context_reset_every(), 0)


if __name__ == "__main__":
    unittest.main()
