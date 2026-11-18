"""What the user sees on screen: plain words, one result line per search."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from src.search_phase import friendly_browser_message
from tests import test_search_flow as flow


class TestFriendlyMessages(unittest.TestCase):
    """Real messages taken from the UAE run's log."""

    def test_blocks(self):
        self.assertEqual(
            friendly_browser_message(
                "BLOCKED: Blocked: body_forbidden, http_403_text (page_status=200 logos=0 heading=0 "
                "graphql_ok=4 signals=body_forbidden,http_403_text) (attempt 1/10)"
            ),
            "ensun blocked this connection (403 Forbidden) -> getting a fresh connection",
        )
        self.assertEqual(
            friendly_browser_message("RATE_LIMIT: page 6 request blocked (HTTP 429) (attempt 1/10)"),
            "ensun says: too many requests (429) -> getting a fresh connection",
        )
        self.assertIn(
            "connection to ensun dropped",
            friendly_browser_message("BLOCKED: Page.goto: net::ERR_CONNECTION_RESET at https://ensun.io/x"),
        )

    def test_vpn(self):
        self.assertEqual(
            friendly_browser_message("VPN_ROTATE [ASIA/United Arab Emirates]: requesting server change..."),
            "VPN: switching to a new server...",
        )
        self.assertEqual(
            friendly_browser_message("VPN_ROTATE [ASIA/United Arab Emirates]: 195.242.214.115 -> 212.8.253.163 (11.2s)"),
            "VPN: new server ready (11s)",
        )
        self.assertEqual(
            friendly_browser_message(
                "VPN_ROTATE [ASIA/United Arab Emirates]: coalesced 205.147.17.40 -> 195.242.214.115 (321.4s, age 0.3s)"
            ),
            "VPN: new server ready (321s)",
        )
        self.assertEqual(
            friendly_browser_message("VPN_COOKIES: Search results detected — cookies saved"),
            "ensun works again -> continuing",
        )

    def test_technical_lines_hidden_from_screen(self):
        for msg in (
            "FAST: 81 companies from 9 page(s) — ensun answered pages 2-9 in 1.1s each",
            "PAGINATE: reached 100 company cap on page 10 (ensun free limit)",
            "WAIT: page_ready 4/30 logos=0 graphql_ok=0 heading=0",
            "RETRY 2/10: cookies refreshed after VPN change",
            "VPN_COOKIES: Opening tab — reload until search results appear",
        ):
            self.assertIsNone(friendly_browser_message(msg), msg)

    def test_unknown_messages_shown_unchanged(self):
        msg = "VPN_ROTATE [ASIA/India]: skipped — service unavailable"
        self.assertEqual(friendly_browser_message(msg), msg)
        self.assertEqual(friendly_browser_message("WARN: something new"), "WARN: something new")


class TestScreenOutput(unittest.TestCase):
    """Run the scripted flow and check the exact lines printed on screen."""

    def test_screen_lines(self):
        printed: list[str] = []

        def capture(self_, message, *, console=True):
            if console:
                printed.append(message)

        helper = flow.SearchFlowTest()
        helper.setUp()
        browser = flow.FakeBrowser()
        runner = helper.make_runner("Screenland", browser)
        with patch("src.search_phase.interruptible_sleep", flow._no_sleep), patch(
            "src.search_phase.register_shutdown_handler", lambda *_a: None
        ), patch("src.search_phase.SearchPhaseRunner._log", capture):
            runner.run()
        screen = [line for line in "\n".join(printed).splitlines() if line.strip()]
        text = "\n".join(screen)
        self.assertIn('[1 of 5] 1.1 Seeds supplier | "agri input dealer"', text)
        self.assertIn("    0 of 0 found | 0 new | total 0 | 0 pages in", text)
        self.assertIn("    100 of 160 found | 100 new | total 100 | 10 pages in", text)
        self.assertIn("    ~60 more on ensun -> will use filters in Pass 2", text)
        self.assertIn("    only 10 more on ensun -> too few to be worth extra searches", text)
        self.assertIn("PASS 2: using filters for 1 big term(s)", text)
        self.assertIn('[Pass 2: 1 of 1] 1.3 Crop protection supplier | "Pesticide" | ~60 more on ensun', text)
        self.assertIn("      Chemicals: 90 of 90 found | 0 new | total 200 | 9 pages in", text)
        self.assertIn("      Agriculture: 63 of 63 found | 53 new | total 253 | 7 pages in", text)
        self.assertIn("      Plastics: 7 of 7 found | 7 new | total 260 | 1 page in", text)
        self.assertIn("TERM DONE: 3 search(es) | 160 companies for this term | 60 new | total 260", text)
        for noise in ("FAST:", "pages=", "reused/skipped", "s/page", "Round "):
            self.assertNotIn(noise, text)
        print("\n--- screen output ---\n" + text)


if __name__ == "__main__":
    unittest.main()
