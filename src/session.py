from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Awaitable, TypeVar

T = TypeVar("T")

from playwright.async_api import Browser, BrowserContext, Page, TimeoutError as PlaywrightTimeoutError, async_playwright

from src.browser_errors import (
    BlockedError,
    FetchDiagnostics,
    PageNotReadyError,
    SEARCH_HEADING_PATTERN,
    detect_block_signals,
    filter_false_positive_block_signals,
    filter_false_positive_profile_block_signals,
    is_browser_driver_error,
    is_real_bot_block,
    is_search_page_ready,
    is_verified_empty,
    is_rate_limited,
)
from src.config_loader import settings
from src.facets import FACET_OPERATION, Facets, facet_request_matches, parse_facet_body
from src.graphql.extract import (
    SEARCH_RESULTS_PAGE_SIZE,
    collect_companies_from_search,
    company_dedupe_key,
    extract_companies_from_page,
    merge_company_lists,
)
from src.graphql.parser import build_search_url, extract_companies_from_responses
from src.graphql.search_records import parse_result_records, record_to_company
from src.locations import (
    LocationEntry,
    extract_locations_from_graphql_payload,
    infer_level,
    infer_parent_triple,
    label_from_parts,
    parse_location_triple,
    triple_from_parts,
)
from src.storage.session_db import SessionStore


async def _accept_consent(page: Page) -> None:
    for label in ("Accept all", "Allow necessary", "Accept"):
        try:
            btn = page.get_by_role("button", name=label)
            if await btn.count() > 0:
                await btn.first.click(timeout=3000)
                await page.wait_for_timeout(500)
                return
        except Exception:
            continue


async def _collect_cookies(context) -> list[dict[str, Any]]:
    cookies = await context.cookies()
    return cookies


_HTTP_DOM_OVERLAY_KEYS = (
    "website_url",
    "linkedin_url",
    "facebook_url",
    "twitter_url",
    "google_maps_url",
    "hq_city",
    "hq_state",
    "hq_country",
    "location_count",
    "countries_operating_count",
    "certificates",
    "employees",
    "specialised_areas",
)


def _overlay_http_dom(dom_data: dict[str, Any], http_dom: dict[str, Any] | None) -> dict[str, Any]:
    if not http_dom:
        return dom_data
    for key in _HTTP_DOM_OVERLAY_KEYS:
        hval = http_dom.get(key)
        dval = dom_data.get(key)
        if hval in (None, "", []) and dval not in (None, "", []):
            continue
        if hval not in (None, "", []):
            if dval in (None, "", []) or key in (
                "linkedin_url",
                "facebook_url",
                "twitter_url",
                "google_maps_url",
                "website_url",
            ):
                dom_data[key] = hval
    return dom_data


class BrowserSession:
    def __init__(self) -> None:
        self.session_store = SessionStore()
        self.settings = settings()
        self._browser: Browser | None = None
        self._playwright = None
        self._search_context: BrowserContext | None = None
        self._search_page: Page | None = None
        self._consent_done: bool = False
        self._search_fetch_count: int = 0
        self._profile_context: BrowserContext | None = None
        self._profile_page: Page | None = None
        self._profile_consent_done: bool = False
        self._profile_fetch_count: int = 0
        self._profile_graphql_responses: list[Any] = []
        self._profile_graphql_statuses: list[int] = []
        self._graphql_responses: list[Any] = []
        self._graphql_statuses: list[int] = []
        self._graphql_errors: list[int] = []
        self._graphql_ok_count: int = 0
        # (request variables, response body) of every getExpectedSearchResultCount
        # call seen on the search page — the filter-panel counts.
        self._facet_payloads: list[tuple[Any, Any]] = []
        self._page_status: int | None = None
        self._last_diagnostics: FetchDiagnostics | None = None
        self._log_callback: Callable[[str], None] | None = None
        self._headless: bool = True
        self._scrape_id: str = "ensun"
        self._status_reporter = None
        self._profile_coordinator = None
        self._profile_worker_key: str = "w0"
        self._interrupt_check: Callable[[], bool] | None = None
        # time.time() when the current ensun request began (see note_request_started).
        self._request_started_at: float | None = None

    def set_interrupt_check(self, check: Callable[[], bool] | None) -> None:
        self._interrupt_check = check

    def _check_interrupted(self) -> None:
        from src.shutdown import ShutdownRequested, is_shutdown_requested

        if is_shutdown_requested():
            raise ShutdownRequested()
        if self._interrupt_check is not None and self._interrupt_check():
            raise ShutdownRequested()

    def set_profile_coordinator(self, coordinator, worker_key: str) -> None:
        self._profile_coordinator = coordinator
        self._profile_worker_key = worker_key
        if coordinator is not None:
            coordinator.register_browser(self)

    async def prepare_profile_fetch(self) -> None:
        if self._profile_coordinator is None:
            return
        # Wait for recovery to fully complete before any Playwright or HTTP call.
        # wait_recovery_done() blocks until VPN+cookie refresh finishes and the
        # shared browser is back up; wait_rate_limit_cooldown() then enforces the
        # post-recovery settle window.
        await self._profile_coordinator.wait_recovery_done()
        await self._profile_coordinator.wait_rate_limit_cooldown()
        changed = await self._profile_coordinator.wait_for_fresh_cookies(
            self.session_store,
            self._profile_worker_key,
        )
        if changed and self._profile_context is not None:
            await self._reset_profile_session(save_cookies=False)

    async def _run_cookie_refresh_protected(
        self,
        refresh_fn: Callable[[], Awaitable[Any]],
    ) -> Any:
        if self._profile_coordinator is None:
            return await refresh_fn()
        coord = self._profile_coordinator

        async def _do_refresh() -> Any:
            lock = coord.cookie_refresh_lock()
            await asyncio.to_thread(lock.acquire)
            try:
                return await refresh_fn()
            finally:
                await asyncio.to_thread(lock.release)

        return await coord.run_cookie_refresh_exclusive(_do_refresh)

    def set_status_reporter(self, reporter) -> None:
        self._status_reporter = reporter

    def set_scrape_id(self, scrape_id: str) -> None:
        self._scrape_id = scrape_id.strip() or "ensun"

    async def _report_scraper_status(self, status: str, message: str = "") -> None:
        if self._status_reporter is None:
            return
        try:
            await self._status_reporter.report(status, message)
        except Exception:
            pass

    def set_log_callback(self, callback: Callable[[str], None] | None) -> None:
        self._log_callback = callback

    @property
    def last_diagnostics(self) -> FetchDiagnostics | None:
        return self._last_diagnostics

    def _log(self, message: str) -> None:
        if self._log_callback:
            self._log_callback(message)
        else:
            try:
                print(message, flush=True)
            except UnicodeEncodeError:
                import sys
                safe = message.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(sys.stdout.encoding or "utf-8")
                print(safe, flush=True)

    def _reset_fetch_diagnostics(self) -> None:
        self._graphql_responses = []
        self._graphql_statuses = []
        self._graphql_errors = []
        self._graphql_ok_count = 0
        self._facet_payloads = []
        self._page_status = None
        self._last_diagnostics = None

    def _rate_limit_codes(self) -> list[int]:
        raw = self.settings.get("rate_limit_http_codes") or [429]
        return [int(code) for code in raw]

    def _is_rate_limited(self) -> bool:
        return is_rate_limited(
            page_status=self._page_status,
            graphql_errors=self._graphql_errors,
            rate_limit_codes=self._rate_limit_codes(),
        )

    def _raise_if_rate_limited(self, context: str) -> None:
        if not self._is_rate_limited():
            return
        diagnostics = FetchDiagnostics(
            page_status=self._page_status,
            graphql_statuses=list(self._graphql_statuses),
            graphql_ok_count=self._graphql_ok_count,
            graphql_error_count=len(self._graphql_errors),
            block_signals=["rate_limited"],
        )
        raise BlockedError(
            f"Rate limited ({context}): {diagnostics.summary()}",
            diagnostics=diagnostics,
        )

    def _retry_delay_seconds(self, attempt: int, exc: Exception) -> float:
        base_delay = float(self.settings.get("fetch_retry_base_seconds", 5))
        max_delay = float(self.settings.get("fetch_retry_max_seconds", 60))
        rate_delay = float(self.settings.get("fetch_retry_rate_limit_seconds", 90))
        if isinstance(exc, BlockedError) and self._is_rate_limited_from_exc(exc):
            return max(rate_delay, min(base_delay * (2 ** (attempt - 1)), max_delay))
        return min(base_delay * (2 ** (attempt - 1)), max_delay)

    def _is_rate_limited_from_exc(self, exc: Exception) -> bool:
        diag = getattr(exc, "diagnostics", None)
        if diag is not None:
            return is_rate_limited(
                page_status=diag.page_status,
                graphql_statuses=diag.graphql_statuses,
                rate_limit_codes=self._rate_limit_codes(),
            ) or "rate_limited" in diag.block_signals or any(
                f"page_http_{code}" in diag.block_signals
                or f"graphql_{code}" in diag.block_signals
                for code in self._rate_limit_codes()
            )
        message = str(exc).lower()
        return "429" in message or "rate limit" in message

    def _is_real_bot_block_from_exc(self, exc: Exception) -> bool:
        diag = getattr(exc, "diagnostics", None)
        if isinstance(diag, FetchDiagnostics):
            return is_real_bot_block(diag)
        return False

    def _diag_matches_vpn_block_codes(self, diag: FetchDiagnostics | None) -> bool:
        """
        True when this failure should trigger VPN + cookie recovery.

        Ensun often returns HTTP 200 with forbidden text in the body — page_status
        alone is not enough; block signals must count too.
        """
        if diag is None:
            return True
        codes = self.settings.get("vpn_rotate_block_codes") or [403, 429]
        allowed = {int(code) for code in codes}

        if diag.page_status is not None and diag.page_status in allowed:
            return True

        signals = set(diag.block_signals or [])
        if "rate_limited" in signals and 429 in allowed:
            return True
        if any(f"page_http_{code}" in signals for code in allowed):
            return True
        if any(f"graphql_{code}" in signals for code in allowed):
            return True

        # Soft block: 200 OK page but forbidden/denied body and no search results.
        soft_block_signals = {
            "body_forbidden",
            "http_403_text",
            "body_access_denied",
            "body_captcha",
            "body_verify_you_are_human",
            "body_checking_your_browser",
            "body_cf-browser-verification",
            "body_hcaptcha",
            "body_recaptcha",
        }
        if signals & soft_block_signals:
            if diag.logo_count == 0 and diag.heading_count == 0:
                return 403 in allowed or 429 in allowed
        return False

    @staticmethod
    def _is_network_block_error(exc: Exception) -> bool:
        """True for connection drops that usually mean IP/VPN path is dead."""
        msg = str(exc).lower()
        markers = (
            "err_connection_reset",
            "err_connection_closed",
            "err_connection_refused",
            "err_connection_timed_out",
            "err_network_changed",
            "err_tunnel_connection_failed",
            "err_internet_disconnected",
            "err_proxy_connection_failed",
            "err_address_unreachable",
            "err_timed_out",
            "net::err_",
        )
        return any(marker in msg for marker in markers)

    def _should_use_vpn_recovery(self, exc: Exception) -> bool:
        """VPN rotate on blocks, soft-forbidden pages, or network connection drops."""
        if not self.settings.get("vpn_rotate_on_block", False):
            return False
        if self._is_network_block_error(exc):
            return True
        if isinstance(exc, BlockedError):
            if not self.settings.get("vpn_rotate_on_all_blocks", True):
                return self._is_rate_limited_from_exc(exc)
            return self._diag_matches_vpn_block_codes(
                getattr(exc, "diagnostics", None)
            )
        if isinstance(exc, PageNotReadyError):
            return self._diag_matches_vpn_block_codes(
                getattr(exc, "diagnostics", None)
            )
        return False

    def _should_refresh_cookies_on_retry(self, exc: Exception) -> bool:
        if self._is_rate_limited_from_exc(exc):
            return bool(self.settings.get("cookie_refresh_on_rate_limit", True))
        return True

    def _should_rotate_vpn_on_block(self, exc: Exception) -> bool:
        return self._should_use_vpn_recovery(exc)

    def note_request_started(self) -> None:
        """Remember when the current ensun request began (``time.time()``).

        A VPN switch that finished *before* this moment cannot have cured a
        failure of this request — that switch's server is the one failing.
        """
        self._request_started_at = time.time()

    async def _rotate_vpn_on_block(
        self, exc: Exception, *, failed_since: float | None = None
    ) -> bool:
        """Get a different VPN server for a failure that began at ``failed_since``.

        A recent switch is reused only if it finished after the failure began
        (another scraper already fixed it). The VPN app also reuses switches
        younger than its own coalesce window; if it hands back the server that
        is failing, wait out the rest of that window once, then ask again.
        """
        from src.shutdown import interruptible_sleep
        from src.vpn_client import (
            current_ip_from_status,
            get_status,
            is_available,
            recent_rotate_from_status,
            rotate_vpn,
        )

        base_url = str(
            self.settings.get("vpn_rotate_service_url", "http://127.0.0.1:8765")
        )
        timeout = float(self.settings.get("vpn_rotate_timeout_seconds", 1200))
        coalesce_window = float(
            self.settings.get("vpn_rotate_coalesce_seconds", 180)
        )
        not_before = failed_since if failed_since is not None else self._request_started_at
        max_rounds = max(1, int(self.settings.get("vpn_stale_reuse_max_waits", 2)) + 1)

        if not is_available(base_url):
            self._log(f"VPN_ROTATE [{self._scrape_id}]: service unavailable, skipping")
            return False

        if self._scrape_id == "ensun" or "/" not in self._scrape_id:
            self._log(
                f"VPN_ROTATE [{self._scrape_id}]: invalid scrape id — "
                f"call set_scrape_id('EUROPE/Country') before scraping"
            )
            return False

        async def failed(reason: str) -> bool:
            # The heartbeat repeats the last status every few seconds; without
            # this, the VPN app keeps showing "Requesting VPN server change" for
            # the whole retry back-off after a failed switch.
            await self._report_scraper_status("blocked", f"VPN change failed: {reason}"[:200])
            return False

        result: dict[str, Any] | None = None
        baseline_ip: str | None = None
        for round_no in range(1, max_rounds + 1):
            pre_status = await asyncio.to_thread(get_status, base_url)
            baseline_ip = current_ip_from_status(pre_status)

            self._log(f"VPN_ROTATE [{self._scrape_id}]: requesting server change...")
            await self._report_scraper_status("rotating", "Requesting VPN server change")
            result = await asyncio.to_thread(
                rotate_vpn,
                base_url,
                timeout,
                client_id=self._scrape_id,
                trigger="scraper_block",
                coalesce_seconds=coalesce_window,
                not_before=not_before,
            )
            if not (result and result.get("ok") and result.get("stale_reuse")):
                break
            # The VPN app answered with its last switch — made before this
            # failure began, so its server is the failing one.
            age = float(result.get("coalesce_age_seconds") or 0.0)
            if round_no == max_rounds:
                self._log(
                    f"VPN_ROTATE [{self._scrape_id}]: the VPN app keeps giving back "
                    f"the failing server ({result.get('new_ip')}) — giving up for now"
                )
                return await failed("the VPN app keeps giving back the failing server")
            wait = min(coalesce_window, max(3.0, coalesce_window - age + 3.0))
            self._log(
                f"VPN_WAIT [{self._scrape_id}]: the VPN app gave back the failing server "
                f"{result.get('new_ip')} (switched {age:.0f}s ago) — it allows a new "
                f"server in {wait:.0f}s, waiting"
            )
            await self._report_scraper_status(
                "rotating", f"Waiting {wait:.0f}s for the VPN app to allow a new server"
            )
            await interruptible_sleep(wait)
            self._check_interrupted()

        if not result or not result.get("ok"):
            error = (result or {}).get("error", "no response")
            if (result or {}).get("disabled"):
                self._log(
                    f"VPN_ROTATE [{self._scrape_id}]: skipped — "
                    f"enable 'Rotate on scraper block' in the VPN app"
                )
                return await failed("'Rotate on scraper block' is off in the VPN app")
            # Queue wait can exceed one HTTP chunk — IP may have changed anyway.
            status = await asyncio.to_thread(get_status, base_url)
            recovered = recent_rotate_from_status(
                status,
                max_age_seconds=coalesce_window,
                baseline_ip=baseline_ip,
                not_before=not_before,
            )
            if recovered:
                result = recovered
                self._log(
                    f"VPN_ROTATE [{self._scrape_id}]: recovered recent IP change "
                    f"{recovered.get('old_ip')} -> {recovered.get('new_ip')} "
                    f"(after {error})"
                )
            else:
                warning = (result or {}).get("warning")
                if warning:
                    self._log(f"VPN_ROTATE [{self._scrape_id}]: {warning}")
                else:
                    self._log(f"VPN_ROTATE [{self._scrape_id}]: failed — {error}")
                return await failed(str(warning or error))

        old_ip = result.get("old_ip")
        new_ip = result.get("new_ip")
        # Require a real IP change (or coalesced reuse of a prior real change).
        if not new_ip or (old_ip is not None and new_ip == old_ip):
            self._log(
                f"VPN_ROTATE [{self._scrape_id}]: rejected — IP did not change "
                f"({old_ip} -> {new_ip})"
            )
            return await failed(f"IP did not change ({new_ip})")

        elapsed = result.get("elapsed_seconds", "?")
        warning = result.get("warning")
        if result.get("coalesced"):
            age = result.get("coalesce_age_seconds", "?")
            self._log(
                f"VPN_ROTATE [{self._scrape_id}]: coalesced "
                f"{old_ip} -> {new_ip} ({elapsed}s, age {age}s)"
            )
        else:
            self._log(f"VPN_ROTATE [{self._scrape_id}]: {old_ip} -> {new_ip} ({elapsed}s)")
        if warning:
            self._log(f"VPN_ROTATE [{self._scrape_id}]: note — {warning}")
        await self._report_scraper_status(
            "rotating", f"Rotated to {new_ip} — refreshing cookies"
        )
        return True

    def _should_cookie_refresh_on_block(self, exc: Exception) -> bool:
        if not isinstance(exc, BlockedError):
            return False
        if not self.settings.get("vpn_cookie_refresh_on_block", True):
            return False
        return self._diag_matches_vpn_block_codes(getattr(exc, "diagnostics", None))

    async def refresh_cookies_on_block(self, *, after_vpn: bool = False) -> list[dict[str, Any]]:
        """Visible tab + reload loop on block — does not require VPN to have changed."""
        if after_vpn:
            settle = float(self.settings.get("vpn_rotate_settle_seconds", 5))
            if settle > 0:
                from src.shutdown import interruptible_sleep

                await interruptible_sleep(settle)
        return await self.refresh_cookies_after_vpn(on_block=True)

    async def refresh_cookies_after_vpn(self, *, on_block: bool = False) -> list[dict[str, Any]]:
        """
        Open one tab and reload until search results are visible, then save cookies.
        When on_block=True, runs even if vpn_cookie_refresh_after_rotate is false.

        Profile phase rule: never run two Chromium processes at once.
        """
        if not on_block and not self.settings.get("vpn_cookie_refresh_after_rotate", True):
            return await self.refresh_cookies()

        if self._profile_coordinator is None:
            return await self._refresh_cookies_on_private_browser()

        use_visible = bool(self.settings.get("vpn_cookie_refresh_visible", True))
        # Visible cookie tab needs a headed window. Stop the shared worker browser
        # first so only one Chromium exists, then bring shared back after.
        # with_shared_browser_stopped holds _browser_restart_lock for the entire
        # private-browser window so no other worker can spawn a second Chromium.
        if use_visible and self._headless:
            self._browser = None
            result = await self._profile_coordinator.with_shared_browser_stopped(
                self._refresh_cookies_on_private_browser,
                headless=self._headless,
            )
            self._browser = self._profile_coordinator._shared_browser
            return result
        return await self._refresh_cookies_on_shared_browser()

    async def _load_cookie_tab(self, page: Page, url: str, timeout_seconds: float) -> tuple[str, str]:
        """Load the cookie-tab URL once.

        Returns (outcome, detail): "ok"; "retry" (VPN hop / the page navigated
        itself — not the server's fault); "dead" (no connection to ensun, or no
        page within the time limit); "closed" (the tab was closed).
        """
        try:
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=max(1000, int(timeout_seconds * 1000)),
            )
        except Exception as exc:
            if page.is_closed():
                return "closed", "tab closed"
            msg = str(exc)
            detail = (msg.strip().splitlines() or [type(exc).__name__])[0][:160]
            if (
                "ERR_NETWORK_CHANGED" in msg
                or "ERR_ABORTED" in msg
                or "interrupted by another navigation" in msg
            ):
                self._log(f"VPN_COOKIES: Page load interrupted ({detail}) — trying again")
                return "retry", detail
            if self._is_network_block_error(exc) or isinstance(exc, PlaywrightTimeoutError):
                self._log(f"VPN_COOKIES: Page did not load ({detail})")
                return "dead", detail
            self._log(f"VPN_COOKIES: Page load failed ({detail}) — trying again")
            return "retry", detail
        await _accept_consent(page)
        return "ok", ""

    async def _cookie_tab_has_results(self, page: Page) -> bool:
        """Search results visible? (A page mid-navigation counts as 'not yet'.)"""
        try:
            return await self._interactive_search_page_ready(page)
        except Exception:
            return False

    async def _wait_for_cookie_tab_ready(
        self,
        context: BrowserContext,
        page: Page,
    ) -> list[dict[str, Any]]:
        """
        Load one tab until ensun search results are visible, then save cookies.

        Shared by both the shared-browser (profile phase) and private-browser
        (search phase) cookie refresh paths — only browser lifecycle differs
        between them, not this wait/reload/save logic.

        - Each load may take at most ``vpn_cookie_refresh_nav_timeout_seconds``
          (a dead connection used to leave the tab blank for 120 s).
        - After a load, results get ``vpn_cookie_refresh_reload_seconds`` to
          appear before the next reload (ensun's browser check passes by itself
          in ~4 s; reloading right away interrupted it).
        - ``vpn_cookie_refresh_dead_after_failures`` connection failures in a
          row -> VpnServerUnreachableError: this VPN server can't reach ensun,
          so the caller switches server instead of reloading for 2 minutes.
        """
        from src.browser_errors import VpnServerUnreachableError
        from src.shutdown import interruptible_sleep

        max_seconds = float(self.settings.get("vpn_cookie_refresh_max_seconds", 120))
        reload_seconds = float(self.settings.get("vpn_cookie_refresh_reload_seconds", 10))
        nav_timeout = float(self.settings.get("vpn_cookie_refresh_nav_timeout_seconds", 30))
        dead_after = max(1, int(self.settings.get("vpn_cookie_refresh_dead_after_failures", 2)))
        retry_seconds = max(1.0, float(self.settings.get("vpn_rotate_settle_seconds", 5)))
        poll_seconds = self._page_ready_poll_ms() / 1000.0
        url = f"{self.settings['ensun_base_url']}/search?q=test"

        deadline = time.monotonic() + max_seconds
        next_load_at = time.monotonic()  # first load right away
        loads = 0
        dead_in_a_row = 0
        while True:
            self._check_interrupted()
            now = time.monotonic()
            if now >= deadline or page.is_closed():
                break
            if loads and await self._cookie_tab_has_results(page):
                await page.wait_for_timeout(500)
                await _accept_consent(page)
                cookies = await _collect_cookies(context)
                self.session_store.save_cookies(cookies)
                self._log("VPN_COOKIES: Search results detected — cookies saved")
                await self._report_scraper_status("running", "Cookies ready")
                return cookies

            if now >= next_load_at:
                if loads:
                    self._log("VPN_COOKIES: No results yet — refreshing tab")
                    await self._report_scraper_status(
                        "getting_cookies", "Refreshing tab for cookies"
                    )
                loads += 1
                outcome, detail = await self._load_cookie_tab(
                    page, url, min(nav_timeout, max(1.0, deadline - now))
                )
                if outcome == "closed":
                    break
                if outcome == "dead":
                    dead_in_a_row += 1
                    if dead_in_a_row >= dead_after:
                        raise VpnServerUnreachableError(
                            f"this VPN server can't reach ensun ({detail}; "
                            f"{dead_in_a_row} tries in a row)"
                        )
                    next_load_at = time.monotonic() + retry_seconds
                elif outcome == "retry":
                    next_load_at = time.monotonic() + retry_seconds
                else:
                    dead_in_a_row = 0
                    next_load_at = time.monotonic() + reload_seconds
                continue

            await interruptible_sleep(poll_seconds)

        raise RuntimeError(
            f"Could not get search results within {max_seconds:.0f}s after VPN rotate"
        )

    async def _open_cookie_tab(self, browser: Browser) -> tuple[BrowserContext, Page]:
        """New context + blank tab. Loading happens in _wait_for_cookie_tab_ready,
        inside the caller's try/finally, so a failed load never leaks the tab."""
        context = await browser.new_context(user_agent=self.settings["user_agent"])
        try:
            page = await context.new_page()
        except Exception:
            try:
                await context.close()
            except Exception:
                pass
            raise
        return context, page

    async def _refresh_cookies_on_shared_browser(self) -> list[dict[str, Any]]:
        """One shared Chromium: open tab → wait for results → save cookies → close tab."""
        assert self._profile_coordinator is not None
        await self._profile_coordinator.suspend_all_profile_browsers()
        browser = await self._profile_coordinator.ensure_shared_browser(self._headless)
        self._browser = browser

        self._log(
            "VPN_COOKIES: Opening tab on shared browser — "
            "reload until search results appear"
        )
        await self._report_scraper_status("getting_cookies", "Opening tab for cookies")

        context, page = await self._open_cookie_tab(browser)
        try:
            return await self._wait_for_cookie_tab_ready(context, page)
        finally:
            try:
                await context.close()
            except Exception:
                pass
            self._log("VPN_COOKIES: Cookie tab closed")

    async def _refresh_cookies_on_private_browser(self) -> list[dict[str, Any]]:
        """Search-phase / single-browser path: temporary Chromium for cookies only."""
        use_visible = bool(self.settings.get("vpn_cookie_refresh_visible", True))
        headless = not use_visible

        self._log("VPN_COOKIES: Opening tab — reload until search results appear")
        await self._report_scraper_status("getting_cookies", "Opening tab for cookies")

        pw = await async_playwright().start()
        browser = None
        try:
            browser = await pw.chromium.launch(
                headless=headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context, page = await self._open_cookie_tab(browser)
            return await self._wait_for_cookie_tab_ready(context, page)
        finally:
            if browser and browser.is_connected():
                await browser.close()
            await pw.stop()

    def _block_http_codes(self) -> list[int]:
        raw = self.settings.get("block_http_codes") or [403, 429, 502, 503]
        return [int(code) for code in raw]

    def _empty_page_http_codes(self) -> list[int]:
        raw = self.settings.get("empty_page_http_codes") or [404]
        return [int(code) for code in raw]

    async def _search_heading_count(self, page: Page) -> int:
        return await page.locator(f"text=/{SEARCH_HEADING_PATTERN}/i").count()

    def _page_ready_settings(self) -> tuple[int, int, int]:
        min_graphql = int(self.settings.get("page_ready_min_graphql_ok", 1))
        min_logos = int(self.settings.get("page_ready_min_logos", 3))
        max_polls = int(self.settings.get("page_ready_max_polls", 30))
        return min_graphql, min_logos, max_polls

    def _is_page_ready(
        self,
        *,
        logo_count: int,
        heading_count: int,
        graphql_ok_count: int,
        min_graphql: int,
        min_logos: int,
    ) -> bool:
        return is_search_page_ready(
            page_status=self._page_status,
            logo_count=logo_count,
            heading_count=heading_count,
            graphql_ok_count=graphql_ok_count,
            min_logos=min_logos,
            min_graphql=min_graphql,
            empty_page_http_codes=self._empty_page_http_codes(),
            block_http_codes=self._block_http_codes(),
        )

    async def start(self, headless: bool = True) -> None:
        self._headless = headless
        if self._profile_coordinator is not None:
            # Profile workers share one Chromium — launching N browsers OOM-kills the process.
            self._browser = await self._profile_coordinator.ensure_shared_browser(
                headless
            )
            self._playwright = None
            return
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )

    async def restart_browser(self) -> None:
        """Fully relaunch Playwright Chromium after a dead driver connection."""
        if self._profile_coordinator is not None:
            self._log("BROWSER: restarting shared Playwright Chromium (driver connection lost)")
            self._browser = await self._profile_coordinator.restart_shared_browser(
                self._headless
            )
            self._playwright = None
            return
        await self._restart_browser_unlocked()

    async def _restart_browser_unlocked(self) -> None:
        self._log("BROWSER: restarting Playwright Chromium (driver connection lost)")
        await self.close_search_session(force_cookie_save=False)
        await self.close_profile_session(force_cookie_save=False)
        browser = self._browser
        self._browser = None
        if browser is not None:
            try:
                if browser.is_connected():
                    await browser.close()
            except Exception:
                pass
        pw = self._playwright
        self._playwright = None
        if pw is not None:
            try:
                await pw.stop()
            except Exception:
                pass
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self._headless,
            args=["--disable-blink-features=AutomationControlled"],
        )

    def _browser_alive(self) -> bool:
        # Profile workers share one Chromium owned by the coordinator. Only the
        # coordinator's copy is authoritative — a worker's own self._browser can
        # go stale the instant another worker restarts the shared browser (e.g.
        # during VPN recovery), which used to make every other worker believe
        # its *own* driver had died and trigger a needless second restart.
        # Re-sync from the coordinator first so a still-alive shared browser is
        # picked up silently instead of logging/queuing a bogus restart.
        if self._profile_coordinator is not None:
            if self._profile_coordinator.shared_browser_alive():
                self._browser = self._profile_coordinator.current_shared_browser()
                return True
            return False
        browser = self._browser
        if browser is None:
            return False
        try:
            return bool(browser.is_connected())
        except Exception:
            return False

    async def _ensure_browser(self) -> None:
        if self._profile_coordinator is not None:
            self._browser = await self._profile_coordinator.ensure_shared_browser(
                self._headless
            )
            return
        if not self._browser_alive():
            if self._playwright is not None or self._browser is not None:
                await self.restart_browser()
            else:
                await self.start(headless=self._headless)

    async def _new_context(self):
        await self._ensure_browser()
        assert self._browser is not None
        context = await self._browser.new_context(
            user_agent=self.settings["user_agent"],
        )
        saved = self.session_store.load_cookies()
        if saved:
            await context.add_cookies(saved)
        return context

    def _max_companies_per_combo(self) -> int:
        return int(self.settings.get("max_companies_per_combo", 100))

    def _max_search_pages(self) -> int:
        return int(self.settings.get("max_graphql_pages", 10))

    def _page_ready_poll_ms(self) -> int:
        return int(self.settings.get("page_ready_poll_ms", 700))

    async def _wait_for_page_ready(
        self,
        page: Page,
        min_graphql: int | None = None,
        raise_on_timeout: bool = True,
    ) -> None:
        cfg_min_graphql, min_logos, max_polls = self._page_ready_settings()
        if min_graphql is None:
            min_graphql = cfg_min_graphql
        poll_ms = self._page_ready_poll_ms()

        for i in range(max_polls):
            await page.wait_for_timeout(poll_ms)
            if self._is_rate_limited():
                self._raise_if_rate_limited("page_ready")
            if (
                raise_on_timeout
                and self._page_status in self._block_http_codes()
                and self._page_status not in self._rate_limit_codes()
            ):
                # ensun already refused the page (e.g. 403 Forbidden): waiting 30
                # polls (~21 s) never helped in practice — go straight to recovery.
                diagnostics = await self._collect_diagnostics(page)
                raise BlockedError(
                    f"Blocked: HTTP {self._page_status} ({diagnostics.summary()})",
                    diagnostics=diagnostics,
                )
            logos = await page.locator('img[alt*="Logo"]').count()
            heading = await self._search_heading_count(page)
            graphql_ok = self._graphql_ok_count

            if self._is_page_ready(
                logo_count=logos,
                heading_count=heading,
                graphql_ok_count=graphql_ok,
                min_graphql=min_graphql,
                min_logos=min_logos,
            ):
                return

            if i % 4 == 3:
                self._log(
                    f"WAIT: page_ready {i + 1}/{max_polls} "
                    f"logos={logos} graphql_ok={graphql_ok} heading={heading}"
                )

        await page.wait_for_timeout(1500)
        if self._is_rate_limited():
            self._raise_if_rate_limited("page_ready timeout")
        logos = await page.locator('img[alt*="Logo"]').count()
        heading = await self._search_heading_count(page)
        if self._is_page_ready(
            logo_count=logos,
            heading_count=heading,
            graphql_ok_count=self._graphql_ok_count,
            min_graphql=min_graphql,
            min_logos=min_logos,
        ):
            return
        if raise_on_timeout:
            diagnostics = await self._collect_diagnostics(page)
            raise PageNotReadyError(
                f"Page not ready after {max_polls} polls ({diagnostics.summary()})",
                diagnostics=diagnostics,
            )

    async def _read_response_json(self, response) -> Any | None:
        try:
            return await response.json()
        except Exception:
            try:
                text = await response.text()
                return json.loads(text)
            except Exception:
                return None

    async def _on_search_response(self, response) -> None:
        if "graphql" not in response.url:
            return
        status = response.status
        self._graphql_statuses.append(status)
        if status >= 400:
            self._graphql_errors.append(status)
            return
        body = await self._read_response_json(response)
        if body is not None:
            self._graphql_responses.append(body)
            self._graphql_ok_count += 1
            try:
                request_json = response.request.post_data_json
            except Exception:
                request_json = None
            if isinstance(request_json, dict) and request_json.get("operationName") == FACET_OPERATION:
                self._facet_payloads.append((request_json.get("variables"), body))

    def _current_facets(
        self, search_term: str, company_type: str = "", industry: str = ""
    ) -> Facets | None:
        """Latest filter-panel counts captured for exactly this search."""
        for variables, body in reversed(self._facet_payloads):
            if facet_request_matches(variables, search_term, company_type, industry):
                facets = parse_facet_body(body)
                if facets is not None:
                    return facets
        return None

    async def _wait_for_facets(
        self, page: Page, search_term: str, company_type: str, industry: str
    ) -> Facets | None:
        wait_s = float(self.settings.get("facet_wait_seconds", 8))
        poll_ms = 300
        for _ in range(max(1, int(wait_s * 1000 / poll_ms))):
            facets = self._current_facets(search_term, company_type, industry)
            if facets is not None:
                return facets
            if self._is_rate_limited():
                self._raise_if_rate_limited("facet wait")
            await page.wait_for_timeout(poll_ms)
        return self._current_facets(search_term, company_type, industry)

    async def _wait_for_result_cards(self, page: Page) -> None:
        """ensun says results exist — give the cards time to render."""
        wait_s = float(self.settings.get("results_render_wait_seconds", 15))
        poll_ms = 400
        for _ in range(max(1, int(wait_s * 1000 / poll_ms))):
            if await page.locator('img[alt*="Logo"]').count() > 0:
                return
            if self._is_rate_limited():
                self._raise_if_rate_limited("results render wait")
            await page.wait_for_timeout(poll_ms)

    async def _collect_diagnostics(self, page: Page) -> FetchDiagnostics:
        logos = await page.locator('img[alt*="Logo"]').count()
        heading = await self._search_heading_count(page)
        try:
            title = await page.title()
        except Exception:
            title = ""
        try:
            body_text = await page.inner_text("body")
        except Exception:
            body_text = ""

        patterns = self.settings.get("block_body_patterns") or []
        block_signals = detect_block_signals(title, body_text[:3000], patterns)
        _, min_logos, _ = self._page_ready_settings()
        block_signals = filter_false_positive_block_signals(
            block_signals,
            page_status=self._page_status,
            logo_count=logos,
            heading_count=heading,
            graphql_ok_count=self._graphql_ok_count,
            min_logos=min_logos,
        )
        block_codes = self._block_http_codes()

        if self._page_status is not None and self._page_status in block_codes:
            block_signals.append(f"page_http_{self._page_status}")
        for status in self._graphql_errors:
            if status in block_codes:
                block_signals.append(f"graphql_{status}")

        return FetchDiagnostics(
            page_status=self._page_status,
            graphql_statuses=list(self._graphql_statuses),
            graphql_ok_count=self._graphql_ok_count,
            graphql_error_count=len(self._graphql_errors),
            logo_count=logos,
            heading_count=heading,
            block_signals=block_signals,
            page_title=title,
        )

    def _validate_search_fetch(
        self,
        diagnostics: FetchDiagnostics,
        companies: list[dict[str, Any]],
        facets: Facets | None = None,
        attempt: int = 1,
    ) -> None:
        if diagnostics.block_signals:
            raise BlockedError(
                f"Blocked: {', '.join(diagnostics.block_signals)} ({diagnostics.summary()})",
                diagnostics=diagnostics,
            )

        if not companies and facets is not None:
            # ensun's own count is the ground truth for "is this really empty?"
            if facets.total == 0:
                return
            max_attempts = int(self.settings.get("count_mismatch_max_attempts", 3))
            if attempt >= max_attempts:
                self._log(
                    f"WARN: ensun count says {facets.total} result(s) but no company "
                    f"cards loaded after {attempt} tries — saving as empty"
                )
                return
            raise PageNotReadyError(
                f"ensun count says {facets.total} result(s) but no company cards "
                f"loaded ({diagnostics.summary()})",
                diagnostics=diagnostics,
            )

        if not companies and not is_verified_empty(
            diagnostics,
            len(companies),
            self._block_http_codes(),
            self._empty_page_http_codes(),
        ):
            raise PageNotReadyError(
                f"Empty results but page not verified empty ({diagnostics.summary()})",
                diagnostics=diagnostics,
            )

    async def _collect_profile_diagnostics(
        self,
        page: Page,
        page_status: int | None,
        graphql_statuses: list[int],
        graphql_ok_count: int,
    ) -> FetchDiagnostics:
        h1 = await page.locator("h1").count()
        try:
            title = await page.title()
        except Exception:
            title = ""
        try:
            body_text = await page.inner_text("body")
        except Exception:
            body_text = ""

        patterns = self.settings.get("block_body_patterns") or []
        block_signals = detect_block_signals(title, body_text[:3000], patterns)
        block_signals = filter_false_positive_block_signals(
            block_signals,
            page_status=page_status,
            logo_count=0,
            heading_count=h1,
            graphql_ok_count=graphql_ok_count,
            min_logos=0,
        )
        # Profile pages have no logo count, so the shared filter above can
        # only clear a signal when GraphQL also happened to fire on this
        # exact navigation. A real rendered heading is on its own strong
        # enough evidence that this is a genuine page, not a block — see
        # filter_false_positive_profile_block_signals for why this matters.
        block_signals = filter_false_positive_profile_block_signals(
            block_signals,
            page_status=page_status,
            heading_count=h1,
        )
        block_codes = self._block_http_codes()
        if page_status is not None and page_status in block_codes:
            block_signals.append(f"page_http_{page_status}")
        for status in graphql_statuses:
            if status >= 400 and status in block_codes:
                block_signals.append(f"graphql_{status}")

        return FetchDiagnostics(
            page_status=page_status,
            graphql_statuses=list(graphql_statuses),
            graphql_ok_count=graphql_ok_count,
            graphql_error_count=sum(1 for s in graphql_statuses if s >= 400),
            logo_count=0,
            heading_count=h1,
            block_signals=block_signals,
            page_title=title,
        )

    def _validate_profile_fetch(self, diagnostics: FetchDiagnostics) -> None:
        if diagnostics.block_signals:
            raise BlockedError(
                f"Blocked: {', '.join(diagnostics.block_signals)} ({diagnostics.summary()})",
                diagnostics=diagnostics,
            )
        if diagnostics.heading_count < 1:
            raise PageNotReadyError(
                f"Profile page not ready ({diagnostics.summary()})",
                diagnostics=diagnostics,
            )

    async def _reset_search_session(self, save_cookies: bool = True) -> None:
        await self.close_search_session(force_cookie_save=save_cookies)
        await self._ensure_browser()
        self._search_context = await self._new_context()
        self._search_page = await self._search_context.new_page()
        self._search_page.on("response", self._on_search_response)
        self._consent_done = False
        self._graphql_responses = []
        self._graphql_statuses = []
        self._graphql_errors = []
        self._graphql_ok_count = 0
        self._facet_payloads = []
        self._page_status = None

    async def _ensure_search_page(self) -> Page:
        if not self._browser_alive():
            await self.restart_browser()
        if self._search_page is not None:
            try:
                if not self._search_page.is_closed():
                    return self._search_page
            except Exception:
                pass
        await self._reset_search_session()
        assert self._search_page is not None
        return self._search_page

    async def close_search_session(self, force_cookie_save: bool = False) -> None:
        if self._search_context is not None:
            if force_cookie_save or self._search_fetch_count > 0:
                try:
                    cookies = await _collect_cookies(self._search_context)
                    self.session_store.save_cookies(cookies)
                except Exception:
                    pass
            try:
                await self._search_context.close()
            except Exception:
                pass
        self._search_context = None
        self._search_page = None
        self._consent_done = False
        self._search_fetch_count = 0
        self._graphql_responses = []
        self._graphql_statuses = []
        self._graphql_errors = []
        self._graphql_ok_count = 0
        self._facet_payloads = []
        self._page_status = None

    def _profile_page_ready_poll_ms(self) -> int:
        return int(self.settings.get("profile_page_ready_poll_ms", 400))

    def _profile_page_ready_max_polls(self) -> int:
        return int(self.settings.get("profile_page_ready_max_polls", 25))

    def _profile_tab_settle_ms(self) -> int:
        return int(self.settings.get("profile_tab_settle_ms", 800))

    def _profile_locations_poll_ms(self) -> int:
        return int(self.settings.get("profile_locations_poll_ms", 400))

    def _profile_locations_max_polls(self) -> int:
        return int(self.settings.get("profile_locations_max_polls", 12))

    def _profile_expand_settle_ms(self) -> int:
        return int(self.settings.get("profile_expand_settle_ms", 800))

    def _profile_context_reset_every(self) -> int:
        return int(self.settings.get("profile_context_reset_every", 50))

    def _profile_goto_wait_until(self) -> str:
        return str(self.settings.get("profile_goto_wait_until", "domcontentloaded"))

    def note_profile_fetch_complete(self) -> None:
        self._profile_fetch_count += 1

    async def _on_profile_response(self, response) -> None:
        if "graphql" not in response.url:
            return
        self._profile_graphql_statuses.append(response.status)
        body = await self._read_response_json(response)
        if body is not None:
            self._profile_graphql_responses.append(body)

    def _reset_profile_graphql_capture(self) -> None:
        self._profile_graphql_responses = []
        self._profile_graphql_statuses = []

    async def close_profile_session(self, force_cookie_save: bool = False) -> None:
        if self._profile_context is not None:
            if force_cookie_save or self._profile_fetch_count > 0:
                try:
                    cookies = await _collect_cookies(self._profile_context)
                    self.session_store.save_cookies(cookies)
                except Exception:
                    pass
            try:
                await self._profile_context.close()
            except Exception:
                pass
        self._profile_context = None
        self._profile_page = None
        self._profile_consent_done = False
        self._reset_profile_graphql_capture()

    async def _reset_profile_session(self, save_cookies: bool = True) -> None:
        await self.close_profile_session(force_cookie_save=save_cookies)
        await self._ensure_browser()
        self._profile_context = await self._new_context()
        self._profile_page = await self._profile_context.new_page()
        self._profile_page.on("response", self._on_profile_response)
        self._profile_consent_done = False
        self._reset_profile_graphql_capture()

    async def _ensure_profile_page(self, *, force_reset: bool = False) -> Page:
        if not self._browser_alive():
            await self.restart_browser()
            force_reset = True
        reset_every = self._profile_context_reset_every()
        if force_reset or (
            reset_every > 0 and self._profile_fetch_count >= reset_every
        ):
            if reset_every > 0 and self._profile_fetch_count >= reset_every:
                self._profile_fetch_count = 0
            await self._reset_profile_session()
        elif self._profile_page is not None:
            try:
                if not self._profile_page.is_closed():
                    return self._profile_page
            except Exception:
                pass
        await self._reset_profile_session()
        assert self._profile_page is not None
        return self._profile_page

    async def _close_profile_before_recovery(self) -> None:
        await self.close_profile_session(force_cookie_save=False)

    async def _reset_profile_after_recovery(self) -> None:
        await self.close_profile_session(force_cookie_save=False)
        self._profile_fetch_count = 0

    async def stop(self) -> None:
        if self._profile_coordinator is not None:
            self._profile_coordinator.unregister_browser(self)
        await self.close_search_session(force_cookie_save=True)
        await self.close_profile_session(force_cookie_save=True)
        # Profile workers use a coordinator-owned shared browser — do not close it here.
        if self._profile_coordinator is not None:
            self._browser = None
            self._playwright = None
            return
        browser = self._browser
        self._browser = None
        if browser is not None:
            try:
                if browser.is_connected():
                    await browser.close()
            except Exception:
                pass
        pw = self._playwright
        self._playwright = None
        if pw is not None:
            try:
                await pw.stop()
            except Exception:
                pass

    async def refresh_cookies(self) -> list[dict[str, Any]]:
        """Refresh cookies in a background headless tab (same mode as the main scraper)."""
        await self._ensure_browser()
        context = await self._new_context()
        try:
            page = await context.new_page()
            await page.goto(
                f"{self.settings['ensun_base_url']}/search?q=test",
                wait_until="load",
                timeout=120000,
            )
            await _accept_consent(page)
            poll_ms = self._page_ready_poll_ms()
            for _ in range(20):
                await page.wait_for_timeout(poll_ms)
                logos = await page.locator('img[alt*="Logo"]').count()
                heading = await page.locator(
                    f"text=/{SEARCH_HEADING_PATTERN}/i"
                ).count()
                if logos >= 3 and heading:
                    break
                if logos >= 1 and heading:
                    break
            cookies = await _collect_cookies(context)
            self.session_store.save_cookies(cookies)
            return cookies
        finally:
            # A failed load (dead connection) must not leave the tab open.
            try:
                await context.close()
            except Exception:
                pass

    async def _interactive_search_page_ready(self, page: Page) -> bool:
        if page.is_closed():
            return False
        logos = await page.locator('img[alt*="Logo"]').count()
        heading = await self._search_heading_count(page)
        return (logos >= 3 and heading > 0) or (logos >= 1 and heading > 0)

    async def _interactive_page_blocked(self, page: Page) -> bool:
        if page.is_closed():
            return False
        if await self._interactive_search_page_ready(page):
            return False
        try:
            title = await page.title()
            body_text = await page.inner_text("body")
        except Exception:
            return False
        patterns = self.settings.get("block_body_patterns") or []
        signals = detect_block_signals(title, body_text[:3000], patterns)
        return bool(signals)

    async def _wait_for_interactive_cookie_refresh(
        self,
        page: Page,
        browser: Browser,
    ) -> str:
        """
        Wait until the user finishes CAPTCHA (search results visible) or closes
        the tab/browser. Reloads the tab when a forbidden/block page persists.
        Returns 'ready' or 'closed'.
        """
        poll_ms = self._page_ready_poll_ms()
        refresh_interval = float(
            self.settings.get("interactive_forbidden_refresh_seconds", 15)
        )
        closed = asyncio.Event()
        last_refresh_at = 0.0

        def _mark_closed() -> None:
            closed.set()

        page.on("close", lambda _page: _mark_closed())
        browser.on("disconnected", lambda _browser: _mark_closed())

        self._log(
            "CAPTCHA: Tab open — solve any challenge; "
            "scraping resumes when results load or you close the tab"
        )

        while not closed.is_set():
            if page.is_closed():
                return "closed"
            try:
                if await self._interactive_search_page_ready(page):
                    await page.wait_for_timeout(500)
                    return "ready"
                if await self._interactive_page_blocked(page):
                    now = time.monotonic()
                    if last_refresh_at == 0.0 or (now - last_refresh_at) >= refresh_interval:
                        self._log("CAPTCHA: Forbidden/block page — refreshing tab")
                        try:
                            await page.reload(wait_until="load", timeout=120000)
                            await _accept_consent(page)
                        except Exception as exc:
                            err = str(exc)
                            if "ERR_NETWORK_CHANGED" in err:
                                self._log(
                                    "CAPTCHA: Network changed (VPN switch) — "
                                    "waiting then reloading"
                                )
                                await asyncio.sleep(
                                    float(
                                        self.settings.get(
                                            "vpn_rotate_settle_seconds", 5
                                        )
                                    )
                                )
                                try:
                                    await page.goto(
                                        page.url,
                                        wait_until="load",
                                        timeout=120000,
                                    )
                                    await _accept_consent(page)
                                except Exception as retry_exc:
                                    self._log(
                                        f"CAPTCHA: Reload after network change failed: "
                                        f"{retry_exc}"
                                    )
                            else:
                                self._log(f"CAPTCHA: Tab refresh failed: {exc}")
                        last_refresh_at = time.monotonic()
                        await page.wait_for_timeout(poll_ms)
                        continue
            except Exception:
                if page.is_closed() or not browser.is_connected():
                    return "closed"
            await page.wait_for_timeout(poll_ms)

        return "closed"

    async def refresh_cookies_interactive(self) -> list[dict[str, Any]]:
        """
        Open a visible Chrome tab so you can solve CAPTCHA / consent manually.
        Cookies are saved automatically when search results load or the tab closes.
        """
        self._log(
            "CAPTCHA: Opening visible browser — solve any challenge on ensun.io"
        )
        pw = await async_playwright().start()
        browser = None
        try:
            browser = await pw.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = await browser.new_context(
                user_agent=self.settings["user_agent"],
            )
            saved = self.session_store.load_cookies()
            if saved:
                await context.add_cookies(saved)
            page = await context.new_page()
            await page.goto(
                f"{self.settings['ensun_base_url']}/search?q=test",
                wait_until="load",
                timeout=120000,
            )
            await _accept_consent(page)

            reason = await self._wait_for_interactive_cookie_refresh(page, browser)
            if reason == "ready":
                self._log("CAPTCHA: Search results detected — saving cookies")
            else:
                self._log("CAPTCHA: Tab closed — saving cookies")

            cookies: list[dict[str, Any]] = []
            if browser.is_connected():
                cookies = await _collect_cookies(context)
                self.session_store.save_cookies(cookies)
            elif saved:
                cookies = saved
                self._log("CAPTCHA: Browser already closed — keeping previous cookies")

            if browser.is_connected():
                await context.close()
            self._log("CAPTCHA: Cookies saved — resuming scrape")
            return cookies
        finally:
            if browser and browser.is_connected():
                await browser.close()
            await pw.stop()

    async def recover_from_block(
        self,
        exc: Exception,
        attempt: int,
        max_retries: int,
    ) -> bool:
        """VPN/cookie recovery for HTTP or Playwright blocks (no page fetch)."""
        return await self._recover_from_fetch_error(
            exc,
            attempt,
            max_retries,
            close_before_recovery=self._close_profile_before_recovery,
            reset_after_recovery=self._reset_profile_after_recovery,
        )

    @dataclass
    class _RecoveryPlan:
        """What a block/rate-limit error needs before the fetch can be retried."""

        label: str
        is_block: bool
        rate_limited: bool
        needs_vpn: bool
        rotate_first: bool
        block_cookie_path: bool  # refresh via the visible "reload until results" tab
        refresh_cookies: bool  # refresh via the plain background session reset

    def _classify_recovery(self, exc: Exception) -> "BrowserSession._RecoveryPlan":
        is_network = self._is_network_block_error(exc)
        is_block = isinstance(exc, BlockedError)
        rate_limited = self._is_rate_limited_from_exc(exc)
        needs_vpn = self._should_use_vpn_recovery(exc)
        block_cookie_path = (
            is_block and self._should_cookie_refresh_on_block(exc)
        ) or (
            needs_vpn
            and (isinstance(exc, PageNotReadyError) or is_network)
            and self.settings.get("vpn_cookie_refresh_on_block", True)
        )
        if rate_limited:
            label = "RATE_LIMIT"
        elif is_block or is_network:
            label = "BLOCKED"
        else:
            label = "WAIT"
        return self._RecoveryPlan(
            label=label,
            is_block=is_block,
            rate_limited=rate_limited,
            needs_vpn=needs_vpn,
            rotate_first=bool(self.settings.get("vpn_rotate_before_backoff", True)),
            block_cookie_path=block_cookie_path,
            refresh_cookies=self._should_refresh_cookies_on_retry(exc),
        )

    async def _rotate_vpn_exclusive(
        self, exc: Exception, *, failed_since: float | None = None
    ) -> bool:
        """Rotate the VPN server, serialized through the coordinator when shared."""
        if self._profile_coordinator is not None:
            await self._profile_coordinator.suspend_all_profile_browsers()
            return await self._profile_coordinator.run_vpn_exclusive(
                lambda: self._rotate_vpn_on_block(exc, failed_since=failed_since)
            )
        return await self._rotate_vpn_on_block(exc, failed_since=failed_since)

    async def _refresh_block_cookies(
        self, attempt: int, max_retries: int, *, rotated: bool
    ) -> tuple[bool, bool]:
        """Cookie refresh for a block — visible tab, reload until results load.

        Returns (ok, server_dead): server_dead = this VPN server can't reach
        ensun at all, so only another server can help.
        """
        from src.browser_errors import VpnServerUnreachableError

        try:
            await self._run_cookie_refresh_protected(
                lambda: self.refresh_cookies_on_block(after_vpn=rotated)
            )
            self._log(
                f"RETRY {attempt + 1}/{max_retries}: cookies refreshed"
                + (" after VPN change" if rotated else " (same IP)")
            )
            return True, False
        except Exception as cookie_exc:
            from src.shutdown import ShutdownRequested

            if isinstance(cookie_exc, ShutdownRequested):
                raise
            self._log(
                f"RETRY {attempt + 1}/{max_retries}: cookie refresh failed: {cookie_exc}"
            )
            return False, isinstance(cookie_exc, VpnServerUnreachableError)

    async def _refresh_block_cookies_on_working_server(
        self,
        exc: Exception,
        attempt: int,
        max_retries: int,
        *,
        rotated: bool,
        can_switch: bool,
    ) -> tuple[bool, bool]:
        """Cookie refresh; if the VPN server turns out to be dead, switch server
        (at most ``vpn_dead_server_max_switches`` times) and try again.

        Returns (rotated, cookies_ok).
        """
        switches_left = max(0, int(self.settings.get("vpn_dead_server_max_switches", 2)))
        while True:
            started = time.time()
            cookies_ok, server_dead = await self._refresh_block_cookies(
                attempt, max_retries, rotated=rotated
            )
            if cookies_ok or not server_dead or not can_switch or switches_left <= 0:
                return rotated, cookies_ok
            switches_left -= 1
            self._log(
                f"VPN_DEAD [{self._scrape_id}]: this VPN server can't reach ensun — "
                f"switching to another server"
            )
            # The cookie tab started on the current server after its switch had
            # finished, so that switch must not be reused.
            if not await self._rotate_vpn_exclusive(exc, failed_since=started):
                return rotated, False
            rotated = True

    async def _refresh_session_cookies(
        self, exc: Exception, attempt: int, max_retries: int
    ) -> tuple[bool, bool]:
        """
        Plain background cookie refresh (e.g. captcha/session reset, no VPN
        rotate involved yet). Returns (ok, needs_vpn_escalation) — a navigation
        timeout here means the current server can't reach ensun.io at all, so
        the caller should escalate straight to a VPN rotation.
        """
        use_interactive = (
            bool(self.settings.get("interactive_cookie_refresh_on_captcha", True))
            and self._is_real_bot_block_from_exc(exc)
        )
        refresh_fn = (
            self.refresh_cookies_interactive if use_interactive else self.refresh_cookies
        )
        try:
            await self._run_cookie_refresh_protected(refresh_fn)
            self._log(f"RETRY {attempt + 1}/{max_retries}: session reset, cookies updated")
            return True, False
        except Exception as cookie_exc:
            from src.shutdown import ShutdownRequested

            if isinstance(cookie_exc, ShutdownRequested):
                raise
            self._log(
                f"RETRY {attempt + 1}/{max_retries}: session reset "
                f"(cookie refresh failed: {cookie_exc})"
            )
            is_nav_timeout = isinstance(
                cookie_exc, (PlaywrightTimeoutError, TimeoutError)
            ) or "Timeout" in type(cookie_exc).__name__
            # A dropped connection (reset, DNS failure, ...) means the same thing
            # as a timeout: this server can't reach ensun -> new server.
            is_dead_connection = self._is_network_block_error(cookie_exc)
            escalate = (is_nav_timeout or is_dead_connection) and bool(
                self.settings.get("vpn_rotate_on_block", True)
            )
            return False, escalate

    async def _execute_recovery(
        self, exc: Exception, plan: "BrowserSession._RecoveryPlan", attempt: int, max_retries: int
    ) -> tuple[bool, bool]:
        """Run VPN rotate (if needed) then cookie refresh (if needed) once.

        Returns (rotated, cookies_ok). Never sleeps/backs off itself — the
        caller applies one consistent settle/backoff after this returns.
        """
        rotated = False
        if plan.needs_vpn and plan.rotate_first:
            rotated = await self._rotate_vpn_exclusive(exc)
            if not rotated:
                self._log(
                    f"VPN_ROTATE [{self._scrape_id}]: no server change — "
                    f"not refreshing cookies on same IP"
                )
                return False, False

        if plan.block_cookie_path:
            if plan.needs_vpn and not rotated:
                # Never spend a cookie-refresh cycle on a block without a
                # confirmed fresh IP — it would just reuse the blocked one.
                return rotated, False
            return await self._refresh_block_cookies_on_working_server(
                exc, attempt, max_retries, rotated=rotated, can_switch=plan.needs_vpn
            )

        if plan.refresh_cookies:
            cookies_ok, escalate = await self._refresh_session_cookies(
                exc, attempt, max_retries
            )
            if not escalate:
                return rotated, cookies_ok
            self._log(
                f"RETRY {attempt + 1}/{max_retries}: cookie nav timeout — "
                f"escalating to VPN rotation (server unreachable)"
            )
            rotated = await self._rotate_vpn_exclusive(exc)
            if not rotated:
                return False, False
            rotated, cookies_ok = await self._refresh_block_cookies_on_working_server(
                exc, attempt, max_retries, rotated=True, can_switch=True
            )
            if cookies_ok:
                self._log(f"RETRY {attempt + 1}/{max_retries}: VPN escalation — cookies ready")
            return rotated, cookies_ok

        self._log(
            f"RETRY {attempt + 1}/{max_retries}: session reset"
            + (" (rate limit backoff)" if plan.rate_limited else "")
        )
        return rotated, True

    async def _run_shared_recovery(
        self, exc: Exception, plan: "BrowserSession._RecoveryPlan", attempt: int, max_retries: int
    ) -> tuple[bool, bool]:
        """Run recovery through the coordinator so only one worker at a time
        rotates the VPN / opens the single cookie-refresh tab; everyone else
        just waits for it to finish."""
        assert self._profile_coordinator is not None
        result: tuple[bool, bool] = (False, False)

        async def _shared_recovery() -> None:
            nonlocal result
            self._log(
                "SHARED_RECOVERY: pausing workers + closing browser tabs — "
                "VPN rotate + cookie refresh"
            )
            result = await self._execute_recovery(exc, plan, attempt, max_retries)
            if result[1]:
                self._log("SHARED_RECOVERY: cookies saved — workers resuming")

        await self._profile_coordinator.wait_recovery_done()
        await self._profile_coordinator.wait_rate_limit_cooldown()
        ran = await self._profile_coordinator.maybe_run_shared_recovery(_shared_recovery)
        if not ran:
            self._log("SHARED_RECOVERY: waiting for other worker recovery")
            # Another worker is already recovering (or just finished) —
            # wait_recovery_done() blocks until its VPN+cookie refresh is
            # fully complete, then the cooldown enforces the post-recovery
            # settle window. Nothing more to do ourselves this attempt.
            await self._profile_coordinator.wait_recovery_done()
            await self._profile_coordinator.wait_rate_limit_cooldown()
            return False, False
        return result

    async def _recover_from_fetch_error(
        self,
        exc: Exception,
        attempt: int,
        max_retries: int,
        *,
        close_before_recovery: Callable[[], Any],
        reset_after_recovery: Callable[[], Any],
        reset_on_soft_error: Callable[[], Any] | None = None,
    ) -> bool:
        """
        Shared block/rate-limit recovery for search and profile fetches.
        Returns True when the caller should retry, False when it should re-raise.
        """
        is_fetch_error = isinstance(
            exc,
            (BlockedError, PageNotReadyError, TimeoutError, PlaywrightTimeoutError),
        )
        is_network = self._is_network_block_error(exc)
        if not is_fetch_error and not is_network:
            if attempt >= max_retries:
                return False
            if is_browser_driver_error(exc):
                self._log(f"RETRY {attempt}/{max_retries}: {exc} — restarting browser")
                await self.restart_browser()
                await asyncio.sleep(1.0)
                return True
            self._log(f"RETRY {attempt}/{max_retries}: {exc} — resetting session")
            await (reset_on_soft_error or reset_after_recovery)()
            return True

        if attempt >= max_retries:
            return False

        plan = self._classify_recovery(exc)
        self._log(f"{plan.label}: {exc} (attempt {attempt}/{max_retries})")

        await close_before_recovery()
        if plan.is_block or plan.needs_vpn or is_network:
            await self._report_scraper_status("blocked", str(exc)[:200])

        use_shared = self._profile_coordinator is not None and (
            (plan.needs_vpn and plan.rotate_first)
            or plan.block_cookie_path
            or plan.refresh_cookies
        )
        if use_shared:
            rotated, cookies_ok = await self._run_shared_recovery(
                exc, plan, attempt, max_retries
            )
        else:
            rotated, cookies_ok = await self._execute_recovery(exc, plan, attempt, max_retries)

        await reset_after_recovery()
        delay = self._retry_delay_seconds(attempt, exc)
        settle = float(self.settings.get("vpn_rotate_settle_seconds", 5))
        from src.shutdown import interruptible_sleep

        await interruptible_sleep(max(2.0, settle) if (cookies_ok or rotated) else delay)
        return True

    async def _fetch_with_block_recovery(
        self,
        fetch_once: Callable[[int], Any],
        *,
        close_before_recovery: Callable[[], Any] | None = None,
        reset_after_recovery: Callable[[], Any] | None = None,
        reset_on_soft_error: Callable[[], Any] | None = None,
    ) -> T:
        max_retries = int(self.settings.get("fetch_max_retries", 10))
        last_exc: Exception | None = None
        close_fn = close_before_recovery or (
            lambda: self.close_search_session(force_cookie_save=False)
        )
        reset_fn = reset_after_recovery or (
            lambda: self._reset_search_session(save_cookies=False)
        )

        try:
            for attempt in range(1, max_retries + 1):
                self._check_interrupted()
                # Recovery must know when this attempt began: a VPN switch that
                # finished before it is the server that just failed.
                self.note_request_started()
                try:
                    return await fetch_once(attempt)
                except Exception as exc:
                    last_exc = exc
                    is_fetch_error = isinstance(
                        exc,
                        (BlockedError, PageNotReadyError, TimeoutError, PlaywrightTimeoutError),
                    )
                    if not await self._recover_from_fetch_error(
                        exc,
                        attempt,
                        max_retries,
                        close_before_recovery=close_fn,
                        reset_after_recovery=reset_fn,
                        reset_on_soft_error=reset_on_soft_error,
                    ):
                        if is_fetch_error:
                            raise BlockedError(
                                f"blocked: {exc} after {max_retries} retries",
                                diagnostics=getattr(exc, "diagnostics", None),
                            ) from exc
                        raise
        finally:
            self._request_started_at = None

        if last_exc:
            raise last_exc
        raise RuntimeError("fetch_with_block_recovery exhausted without result")

    async def fetch_search_pages(
        self,
        search_term: str,
        continent_code: str,
        location_triple: str,
        company_type: str = "",
        industry: str = "",
        max_pages: int | None = None,
    ) -> tuple[list[dict[str, Any]], int, Facets | None]:
        """One ensun search: up to 10 pages (100 companies) + filter-panel counts.

        Returns (companies, pages_fetched, facets). ``facets`` is None only if
        ensun never sent its count query for this page.
        """
        await self._ensure_browser()

        max_pages = max_pages or self._max_search_pages()
        max_companies = self._max_companies_per_combo()
        url = build_search_url(
            search_term,
            continent_code,
            location_triple,
            None,
            company_type or None,
            industry or None,
        )

        # Best partial result across attempts (used if pagination keeps stalling).
        best: dict[str, Any] = {"companies": [], "pages": 0}

        async def _fetch_once(attempt: int) -> tuple[list[dict[str, Any]], int, Facets | None]:
            self._reset_fetch_diagnostics()
            page = await self._ensure_search_page()

            nav_response = await page.goto(
                url, wait_until="domcontentloaded", timeout=120000
            )
            self._page_status = nav_response.status if nav_response else None

            if not self._consent_done:
                await _accept_consent(page)
                self._consent_done = True
            await self._pass_browser_check_and_reload(page, url)
            await self._wait_for_page_ready(page)
            facets = await self._wait_for_facets(page, search_term, company_type, industry)
            if facets is not None and facets.total > 0:
                await self._wait_for_result_cards(page)

            fast = None
            if bool(self.settings.get("fast_pagination", True)):
                fast = await self._fetch_pages_fast(page, url, max_pages, max_companies, facets)
            if fast is not None:
                companies, pages_fetched = fast
            else:
                companies, pages_fetched = await _paginate_search_results(
                    page,
                    max_pages=max_pages,
                    max_companies=max_companies,
                    page_size=SEARCH_RESULTS_PAGE_SIZE,
                    log_fn=self._log,
                    rate_limit_check=self._is_rate_limited,
                )
            if self._graphql_responses:
                from_graphql = extract_companies_from_responses(
                    self._graphql_responses
                )
                companies = merge_company_lists(companies, from_graphql)
                pages_fetched = max(pages_fetched, 1)
            if len(companies) > max_companies:
                companies = companies[:max_companies]
                self._log(
                    f"PAGINATE: capped at {max_companies} companies "
                    f"(ensun free limit, {pages_fetched} pages)"
                )

            if facets is None:
                # The count query may have landed while we were paginating.
                facets = self._current_facets(search_term, company_type, industry)
            diagnostics = await self._collect_diagnostics(page)
            self._last_diagnostics = diagnostics
            self._validate_search_fetch(diagnostics, companies, facets, attempt)
            companies, pages_fetched = self._check_complete_or_retry(
                companies,
                pages_fetched,
                facets,
                attempt,
                best,
                diagnostics,
                min(max_companies, max_pages * SEARCH_RESULTS_PAGE_SIZE),
            )

            self._search_fetch_count += 1
            save_every = int(self.settings.get("cookie_save_every_n_fetches", 25))
            if (
                self._search_context is not None
                and save_every > 0
                and self._search_fetch_count % save_every == 0
            ):
                cookies = await _collect_cookies(self._search_context)
                self.session_store.save_cookies(cookies)

            return companies, pages_fetched, facets

        return await self._fetch_with_block_recovery(
            _fetch_once,
            reset_on_soft_error=lambda: self._reset_search_session(save_cookies=True),
        )

    async def _page1_records(self, page: Page) -> list[dict[str, Any]] | None:
        """Result records of the loaded page 1, or None if they can't be found."""
        for _ in range(12):  # React may still be hydrating for a moment
            recs = await page.evaluate(_PAGE1_RECORDS_JS)
            if recs:
                return parse_result_records(json.dumps({"results": recs}))
            await page.wait_for_timeout(250)
        try:
            embedded = await page.evaluate(_EMBEDDED_DATA_JS)
        except Exception:
            embedded = ""
        recs = parse_result_records(embedded)
        return recs or None

    async def _fetch_pages_fast(
        self,
        page: Page,
        url: str,
        max_pages: int,
        max_companies: int,
        facets: Facets | None,
    ) -> tuple[list[dict[str, Any]], int] | None:
        """Read results from ensun's page data instead of clicking through pages.

        Page 1 comes from the records behind the cards; pages 2..N are requested
        directly (the same request ensun's "next page" button makes), paced by
        ``fast_page_delay_seconds``. Live 2026-11-11: 83 of 83 companies in ~10 s
        instead of ~58 s. Returns None (-> classic page clicking) when the data
        can't be found, e.g. after an ensun website change. 429/403 answers raise
        BlockedError so the normal VPN/cookie recovery runs.
        """
        dom_cards = await extract_companies_from_page(page)
        records = await self._page1_records(page)
        if facets is not None and facets.total == 0 and not dom_cards:
            return [], 0
        if not records:
            if dom_cards:
                self._log("FAST: page data not found — falling back to page clicks")
                return None
            return [], 0
        grade_by_id = {c.get("company_id"): c.get("grade") for c in dom_cards if c.get("company_id")}

        companies: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(recs: list[dict[str, Any]]) -> int:
            added = 0
            for rec in recs:
                company = record_to_company(rec)
                cid = company.get("company_id")
                if not cid or cid in seen:
                    continue
                seen.add(cid)
                company["grade"] = grade_by_id.get(cid)
                companies.append(company)
                added += 1
            return added

        add(records)
        pages_fetched = 1
        page_size = SEARCH_RESULTS_PAGE_SIZE
        expected = min(facets.total, max_companies) if facets is not None else max_companies
        last_page = min(max_pages, max(1, math.ceil(expected / page_size)))
        last_count = len(records)
        delay = float(self.settings.get("fast_page_delay_seconds", 1.0))
        block_codes = set(self._block_http_codes())
        sep = "&" if "?" in url else "?"
        from src.shutdown import interruptible_sleep

        request_seconds = 0.0
        for n in range(2, last_page + 1):
            if len(companies) >= max_companies:
                break
            if facets is None and last_count < page_size:
                break  # no count available: a short page means it was the last one
            await interruptible_sleep(delay)
            t_req = time.monotonic()
            result = await page.evaluate(_FETCH_RSC_JS, f"{url}{sep}page={n}")
            request_seconds += time.monotonic() - t_req
            status = int(result.get("status") or 0)
            if status in block_codes or status == 0:
                signals = [f"page_http_{status}"] if status else ["page_fetch_network_error"]
                raise BlockedError(
                    f"page {n} request blocked (HTTP {status or 'network error'})",
                    diagnostics=FetchDiagnostics(page_status=status or None, block_signals=signals),
                )
            if status != 200:
                raise PageNotReadyError(f"page {n} request returned HTTP {status}")
            recs = parse_result_records(result.get("text") or "")
            if not recs:
                self._log(f"FAST: page {n} had no result data — stopping at {len(companies)} companies")
                break
            add(recs)
            last_count = len(recs)
            pages_fetched = n
        extra_pages = pages_fetched - 1
        speed = (
            f" — ensun answered pages 2-{pages_fetched} in {request_seconds / extra_pages:.1f}s each"
            if extra_pages > 0
            else ""
        )
        self._log(f"FAST: {len(companies)} companies from {pages_fetched} page(s){speed}")
        return companies[:max_companies], pages_fetched

    async def _pass_browser_check_and_reload(self, page: Page, url: str) -> None:
        """Let ensun's 429 browser check finish instead of treating it as a block.

        Live 2026-11-11: the first page load on a new IP (or with expired
        cookies) answers 429 with a JS check; in a real browser it passes by
        itself and results appear ~3.5-4 s later. Pages in that just-checked
        state can't turn pages, so after it passes the cookies are saved and
        the URL is loaded again (then 200). If it doesn't pass within
        ``browser_check_grace_seconds`` the 429 stays in place and the normal
        block recovery (VPN rotate + cookie refresh) runs exactly as before.
        """
        if self._page_status not in self._rate_limit_codes():
            return
        grace = float(self.settings.get("browser_check_grace_seconds", 15))
        if grace <= 0:
            return
        poll_ms = 500
        waited = 0.0
        passed = False
        while waited < grace:
            await page.wait_for_timeout(poll_ms)
            waited += poll_ms / 1000
            try:
                logos = await page.locator('img[alt*="Logo"]').count()
                heading = await self._search_heading_count(page)
            except Exception:
                continue
            if logos >= 1 or heading >= 1:
                passed = True
                break
        if not passed:
            self._log(f"BROWSER_CHECK: 429 not passed within {grace:.0f}s — treating as a block")
            return
        if self._search_context is not None:
            try:
                self.session_store.save_cookies(await _collect_cookies(self._search_context))
            except Exception:
                pass
        self._log(f"BROWSER_CHECK: ensun 429 check passed in {waited:.1f}s — reloading, same IP")
        self._reset_fetch_diagnostics()
        nav = await page.goto(url, wait_until="domcontentloaded", timeout=120000)
        self._page_status = nav.status if nav else None

    def _check_complete_or_retry(
        self,
        companies: list[dict[str, Any]],
        pages_fetched: int,
        facets: Facets | None,
        attempt: int,
        best: dict[str, Any],
        diagnostics: FetchDiagnostics,
        max_companies: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """Retry a search whose pagination stalled before collecting what ensun
        says exists (e.g. count 89, but page 5 never loaded -> only 40 saved).

        Live check (2026-11-11): when paging completes, the cards collected
        match ensun's count exactly (82 of 82), so a real shortfall means a
        stalled page turn — usually the first sign of throttling. After
        ``count_mismatch_max_attempts`` tries the best attempt is kept.
        """
        if facets is None or not companies:
            return companies, pages_fetched
        expected = min(facets.total, max_companies)
        tolerance = int(self.settings.get("incomplete_results_tolerance", 2))
        if len(companies) >= expected - tolerance:
            return companies, pages_fetched
        if len(companies) > len(best["companies"]):
            best["companies"], best["pages"] = companies, pages_fetched
        max_attempts = int(self.settings.get("count_mismatch_max_attempts", 3))
        if attempt >= max_attempts:
            self._log(
                f"WARN: only {len(best['companies'])} of {expected} companies loaded after "
                f"{attempt} tries (pagination kept stalling) — keeping the best attempt"
            )
            return best["companies"], best["pages"]
        raise PageNotReadyError(
            f"incomplete results: got {len(companies)} of {expected} "
            f"(pagination stalled on page {pages_fetched + 1})",
            diagnostics=diagnostics,
        )

    async def count_results(
        self,
        search_term: str,
        continent_code: str,
        location_triple: str,
        company_type: str,
        industries: list[str],
    ) -> dict[str, int]:
        """ensun's own count for each industry filter, without loading result pages.

        Uses the same GraphQL count query ensun's filter panel sends
        (getExpectedSearchResultCount), from inside an ensun.io page so cookies
        and origin are the browser's own. Verified live 2026-11-11: counts match
        the filter panel exactly (pesticide/India/Manufacturer: Chemicals 539,
        Agriculture 105, Consumer Goods 89; plus Plastics 27, Other 27,
        Wholesale 7 that the panel's top 5 never shows).
        """
        if not industries:
            return {}
        await self._ensure_browser()
        base = str(self.settings["ensun_base_url"]).rstrip("/")
        graphql_url = str(self.settings.get("graphql_url"))
        concurrency = max(1, int(self.settings.get("industry_count_concurrency", 4)))
        country, state, city = parse_location_triple(location_triple)

        def query_input(industry: str) -> dict[str, Any]:
            qi: dict[str, Any] = {
                "query": search_term,
                "continents": [continent_code],
                "locations": [{"country": country, "state": state, "city": city}],
                "industries": [industry],
            }
            if company_type:
                qi["categories"] = [company_type]
            return qi

        async def _count_once(_attempt: int) -> dict[str, int]:
            self._reset_fetch_diagnostics()
            page = await self._ensure_search_page()
            if not (page.url or "").startswith(base):
                # Fresh session after recovery: the API call needs an ensun.io page.
                url = build_search_url(
                    search_term, continent_code, location_triple, None, company_type or None
                )
                nav = await page.goto(url, wait_until="domcontentloaded", timeout=120000)
                self._page_status = nav.status if nav else None
                if not self._consent_done:
                    await _accept_consent(page)
                    self._consent_done = True
                await self._pass_browser_check_and_reload(page, url)
                await self._wait_for_page_ready(page)
            results = await page.evaluate(
                _COUNT_JS,
                [graphql_url, _COUNT_QUERY, [query_input(i) for i in industries], concurrency],
            )
            statuses = [int(r.get("status") or 0) for r in results]
            block_codes = set(self._block_http_codes())
            blocked = sorted({s for s in statuses if s in block_codes or s == 0})
            if blocked:
                diag = FetchDiagnostics(
                    page_status=self._page_status,
                    graphql_statuses=statuses,
                    graphql_error_count=sum(1 for s in statuses if s >= 400 or s == 0),
                    block_signals=[f"graphql_{s}" for s in blocked if s] or ["count_network_error"],
                )
                raise BlockedError(f"count check blocked (HTTP {blocked})", diagnostics=diag)
            bad = [i for i, r in zip(industries, results) if r.get("total") is None]
            if bad:
                errors = {r.get("err") for r in results if r.get("err")}
                raise PageNotReadyError(
                    f"count check returned no total for {bad[:3]} ({'; '.join(e for e in errors if e)[:200]})",
                    diagnostics=FetchDiagnostics(page_status=self._page_status, graphql_statuses=statuses),
                )
            return {i: int(r["total"]) for i, r in zip(industries, results)}

        return await self._fetch_with_block_recovery(
            _count_once,
            reset_on_soft_error=lambda: self._reset_search_session(save_cookies=True),
        )

    async def warm_up_cookies(self) -> bool:
        """First-run bootstrap: pass ensun's browser check once and save cookies.

        Without a saved check cookie, ensun answers the first page load with a
        429 challenge, which the block-recovery logic would treat as a block
        (and rotate the VPN for nothing). Returns True when results loaded.
        """
        if self.session_store.load_cookies():
            return True
        await self._ensure_browser()
        context = await self._new_context()
        page = await context.new_page()
        ok = False
        try:
            await page.goto(
                f"{self.settings['ensun_base_url']}/search?q=agriculture",
                wait_until="domcontentloaded",
                timeout=120000,
            )
            await _accept_consent(page)
            wait_s = float(self.settings.get("warm_up_max_seconds", 60))
            for _ in range(int(wait_s / 1.0)):
                await page.wait_for_timeout(1000)
                logos = await page.locator('img[alt*="Logo"]').count()
                heading = await page.locator(f"text=/{SEARCH_HEADING_PATTERN}/i").count()
                if logos >= 1 and heading:
                    ok = True
                    break
            self.session_store.save_cookies(await _collect_cookies(context))
        finally:
            await context.close()
        self._log(
            "COOKIES: warm-up done — ensun browser check passed"
            if ok
            else "COOKIES: warm-up finished without visible results (will rely on block recovery)"
        )
        return ok

    async def capture_locations(
        self,
        continent_code: str,
        country_name: str,
        country_triple: str,
        probe_term: str | None = None,
    ) -> list[LocationEntry]:
        if not self._browser:
            await self.start()
        assert self._browser is not None

        term = probe_term or settings().get("location_probe_term", "packaging")
        url = build_search_url(term, continent_code, country_triple)
        context = await self._new_context()
        page = await context.new_page()
        captured: list[Any] = []

        async def on_response(response):
            if "graphql" not in response.url:
                return
            body = await self._read_response_json(response)
            if body is not None:
                captured.append(body)

        page.on("response", on_response)
        await page.goto(url, wait_until="load", timeout=120000)
        await _accept_consent(page)
        await self._wait_for_page_ready(page, raise_on_timeout=False)

        entries: list[LocationEntry] = [
            LocationEntry(
                triple=country_triple,
                label=country_name,
                level="country",
                parent_triple=None,
            )
        ]
        seen = {country_triple}

        def _add(entry: LocationEntry) -> None:
            if entry.triple in seen:
                return
            seen.add(entry.triple)
            entries.append(entry)

        for payload in captured:
            for entry in extract_locations_from_graphql_payload(payload):
                _add(entry)

        cookies = await _collect_cookies(context)
        self.session_store.save_cookies(cookies)
        await context.close()
        return entries

    async def _wait_for_profile_ready(self, page: Page) -> None:
        poll_ms = self._profile_page_ready_poll_ms()
        max_polls = self._profile_page_ready_max_polls()
        selector_timeout = min(poll_ms * max_polls, 15000)
        try:
            await page.wait_for_selector("h1", timeout=selector_timeout)
            await page.wait_for_timeout(min(poll_ms, 500))
            return
        except Exception:
            pass

        for _ in range(max_polls):
            await page.wait_for_timeout(poll_ms)
            h1 = await page.locator("h1").count()
            if h1 > 0:
                await page.wait_for_timeout(min(poll_ms, 500))
                return

        raise PageNotReadyError(
            f"Profile page not ready after {max_polls} polls",
            diagnostics=FetchDiagnostics(heading_count=0),
        )

    async def _goto_profile_page(
        self,
        page: Page,
        profile_url: str,
    ) -> int | None:
        wait_until = self._profile_goto_wait_until()
        settle = float(self.settings.get("vpn_rotate_settle_seconds", 5))
        last_exc: Exception | None = None

        for nav_attempt in range(1, 4):
            try:
                nav_response = await page.goto(
                    profile_url, wait_until=wait_until, timeout=120000
                )
                page_status = nav_response.status if nav_response else None
                if not self._profile_consent_done:
                    await _accept_consent(page)
                    self._profile_consent_done = True
                try:
                    await self._wait_for_profile_ready(page)
                except PageNotReadyError:
                    if wait_until != "load":
                        nav_response = await page.goto(
                            profile_url, wait_until="load", timeout=120000
                        )
                        page_status = nav_response.status if nav_response else None
                        await self._wait_for_profile_ready(page)
                    else:
                        raise
                return page_status
            except Exception as exc:
                last_exc = exc
                err = str(exc)
                if "ERR_NETWORK_CHANGED" in err and nav_attempt < 3:
                    self._log(
                        f"PROFILE_NAV: network changed — waiting {settle:.0f}s "
                        f"then retry ({nav_attempt}/3)"
                    )
                    from src.shutdown import interruptible_sleep

                    await interruptible_sleep(settle)
                    continue
                raise

        if last_exc:
            raise last_exc
        raise RuntimeError("profile navigation failed without exception")

    async def _click_profile_tab(self, page: Page, name: str) -> bool:
        try:
            already_active = await page.evaluate(
                """
                (tabName) => {
                  const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
                  const hit = tabs.find(
                    (t) => (t.innerText || '').trim() === tabName
                  );
                  return !!(hit && hit.getAttribute('aria-selected') === 'true');
                }
                """,
                name,
            )
            if already_active:
                return True
        except Exception:
            pass

        settle_ms = self._profile_tab_settle_ms()
        selectors = [
            page.get_by_role("tab", name=name),
            page.locator("aside, nav").get_by_text(name, exact=True),
            page.locator(f'a[href="#{name.lower()}"]'),
            page.locator(f'button:has-text("{name}")'),
            page.get_by_text(name, exact=True),
        ]
        for loc in selectors:
            try:
                if await loc.count() > 0:
                    await loc.first.click(timeout=4000)
                    await page.wait_for_timeout(settle_ms)
                    return True
            except Exception:
                continue
        return False

    async def _go_to_locations_section(self, page: Page) -> None:
        clicked = await self._click_profile_tab(page, "Locations")
        if not clicked:
            try:
                await page.locator("#locations").scroll_into_view_if_needed(timeout=5000)
                await page.wait_for_timeout(self._profile_tab_settle_ms())
            except Exception:
                pass

        poll_ms = self._profile_locations_poll_ms()
        for _ in range(self._profile_locations_max_polls()):
            ready = await page.evaluate(
                """
                () => {
                  const hq = Array.from(document.querySelectorAll('h2,h3,h4')).find(
                    (el) => (el.innerText || '').startsWith('Headquarter of')
                  );
                  if (!hq) return false;
                  let block = hq.parentElement;
                  for (let i = 0; i < 18 && block; i++) {
                    const t = block.innerText || '';
                    if (t.includes('City:') && t.includes('Country:')) {
                      const hasLink = block.querySelector(
                        'a[href*="linkedin.com"], a[href*="facebook.com"], a[href*="twitter.com"], a[href*="x.com"], iframe[src*="google.com/maps"]'
                      );
                      return !!hasLink;
                    }
                    block = block.parentElement;
                  }
                  return false;
                }
                """
            )
            if ready:
                return
            await page.wait_for_timeout(poll_ms)

    async def _expand_profile_sections(
        self,
        page: Page,
        *,
        skip_certificates: bool = False,
        skip_employees: bool = False,
    ) -> None:
        settle_ms = self._profile_expand_settle_ms()
        tabs: list[str] = []
        if not skip_certificates:
            tabs.append("Certificates")
        if not skip_employees:
            tabs.append("Employees")
        for tab in tabs:
            await self._click_profile_tab(page, tab)
            await page.wait_for_timeout(settle_ms)

        if skip_employees:
            return

        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
            await page.wait_for_timeout(settle_ms)
        except Exception:
            pass

        try:
            employees_visible = await page.evaluate(
                """
                () => {
                  for (const card of document.querySelectorAll('[class*="MuiPaper"], [class*="mui-"]')) {
                    const t = card.innerText || '';
                    if (t.includes('@') && t.length < 500) return true;
                  }
                  return false;
                }
                """
            )
        except Exception:
            employees_visible = False

        if not employees_visible:
            for label in ("Show employees >", "See employees >"):
                try:
                    buttons = page.get_by_text(label, exact=False)
                    count = await buttons.count()
                    for i in range(min(count, 6)):
                        try:
                            await buttons.nth(i).click(timeout=2000)
                            await page.wait_for_timeout(settle_ms)
                        except Exception:
                            continue
                except Exception:
                    pass

    async def fetch_profile_page(
        self,
        profile_url: str,
        *,
        http_dom: dict[str, Any] | None = None,
    ) -> tuple[list[Any], dict[str, Any]]:
        await self._ensure_browser()

        from src.graphql.profile_dom import extract_profile_from_page

        skip_locations = bool(
            http_dom
            and http_dom.get("website_url")
            and http_dom.get("google_maps_url")
            and (
                http_dom.get("linkedin_url")
                or http_dom.get("facebook_url")
                or http_dom.get("twitter_url")
            )
        )
        skip_certificates = bool(http_dom and http_dom.get("certificates"))
        skip_employees = bool(
            http_dom
            and (
                http_dom.get("employees")
                or not (http_dom.get("employee_total_visible") or 0)
            )
        )

        async def _fetch_once(_attempt: int) -> tuple[list[Any], dict[str, Any]]:
            await self.prepare_profile_fetch()
            force_reset = _attempt > 1
            page = await self._ensure_profile_page(force_reset=force_reset)
            self._reset_profile_graphql_capture()

            page_status = await self._goto_profile_page(page, profile_url)

            diagnostics = await self._collect_profile_diagnostics(
                page,
                page_status,
                self._profile_graphql_statuses,
                len(self._profile_graphql_responses),
            )
            self._validate_profile_fetch(diagnostics)

            dom_data = await extract_profile_from_page(page)

            if not skip_locations:
                await self._go_to_locations_section(page)
                from src.graphql.profile_locations import (
                    extract_locations_tab,
                    merge_locations_into_profile,
                )

                loc_data = await extract_locations_tab(page)
                dom_data = merge_locations_into_profile(dom_data, loc_data)

            await self._expand_profile_sections(
                page,
                skip_certificates=skip_certificates,
                skip_employees=skip_employees,
            )

            dom_data = _overlay_http_dom(dom_data, http_dom)

            save_every = int(self.settings.get("cookie_save_every_n_fetches", 25))
            next_count = self._profile_fetch_count + 1
            if (
                self._profile_context is not None
                and save_every > 0
                and next_count % save_every == 0
            ):
                cookies = await _collect_cookies(self._profile_context)
                self.session_store.save_cookies(cookies)

            graphql_responses = list(self._profile_graphql_responses)
            return graphql_responses, dom_data

        result = await self._fetch_with_block_recovery(
            _fetch_once,
            close_before_recovery=self._close_profile_before_recovery,
            reset_after_recovery=self._reset_profile_after_recovery,
        )
        self.note_profile_fetch_complete()
        return result

    async def capture_graphql_baseline(self, sample_url: str | None = None) -> int:
        if not self._browser:
            await self.start()
        assert self._browser is not None
        url = sample_url or f"{self.settings['ensun_base_url']}/search?q=amino%20acid"
        is_profile = "/company/" in (url or "")
        context = await self._new_context()
        page = await context.new_page()
        saved_count = 0

        async def on_response(response):
            nonlocal saved_count
            if "graphql" not in response.url:
                return
            try:
                req = response.request
                post = req.post_data
                payload = json.loads(post) if post else {"url": response.url}
                body = await self._read_response_json(response)
                if body is None:
                    return
                op = payload.get("operationName") or f"op_{saved_count}"
                from src.locations import save_captured_graphql

                save_captured_graphql(str(op), payload, body)
                saved_count += 1
            except Exception:
                pass

        page.on("response", on_response)
        await page.goto(url, wait_until="load", timeout=120000)
        await _accept_consent(page)
        if is_profile:
            await self._wait_for_profile_ready(page)
        else:
            await self._wait_for_page_ready(page, raise_on_timeout=False)
        cookies = await _collect_cookies(context)
        self.session_store.save_cookies(cookies)
        await context.close()
        return saved_count


# Fast pagination (see BrowserSession._fetch_pages_fast).
# Page-1 records: the `results` prop of the React component that renders the cards.
_PAGE1_RECORDS_JS = r"""
() => {
  const img = Array.from(document.querySelectorAll('img[alt*="Logo"]')).find(i => i.alt !== "ens's Logo");
  if (!img) return null;
  const key = Object.keys(img).find(k => k.startsWith('__reactFiber$'));
  let fiber = key ? img[key] : null;
  for (let lvl = 0; fiber && lvl < 80; lvl++) {
    const p = fiber.memoizedProps;
    if (p && Array.isArray(p.results) && p.results.length && p.results[0] && p.results[0].company) {
      try { return JSON.parse(JSON.stringify(p.results)); } catch (e) { return null; }
    }
    fiber = fiber.return;
  }
  return null;
}
"""
_EMBEDDED_DATA_JS = "() => (self.__next_f || []).map(x => (x && x[1]) || '').join('')"
# Pages 2..N: the same request ensun's own "next page" button makes.
_FETCH_RSC_JS = r"""
async (url) => {
  try {
    const r = await fetch(url, {headers: {'RSC': '1'}, credentials: 'include'});
    return {status: r.status, text: await r.text()};
  } catch (e) {
    return {status: 0, text: '', error: String(e).slice(0, 200)};
  }
}
"""

_COUNT_QUERY = (
    "query getExpectedSearchResultCount($queryInput: SearchQueryInput!) {\n"
    "  getExpectedSearchResultCount(queryInput: $queryInput) {\n    total\n  }\n}"
)

# Runs inside an ensun.io page: N parallel workers over the list of query inputs.
_COUNT_JS = """
async ([url, query, inputs, concurrency]) => {
  const out = new Array(inputs.length);
  let next = 0;
  async function worker() {
    while (true) {
      const i = next++;
      if (i >= inputs.length) return;
      try {
        const r = await fetch(url, {
          method: 'POST',
          credentials: 'include',
          headers: {'content-type': 'application/json'},
          body: JSON.stringify({operationName: 'getExpectedSearchResultCount', query,
                                variables: {queryInput: inputs[i]}}),
        });
        let total = null, err = null;
        try {
          const body = await r.json();
          const node = body && body.data && body.data.getExpectedSearchResultCount;
          total = node && typeof node.total === 'number' ? node.total : null;
          if (body && body.errors) err = JSON.stringify(body.errors).slice(0, 200);
        } catch (e) { err = 'invalid json'; }
        out[i] = {status: r.status, total, err};
      } catch (e) {
        out[i] = {status: 0, total: null, err: String(e).slice(0, 200)};
      }
    }
  }
  await Promise.all(Array.from({length: Math.min(concurrency, inputs.length)}, worker));
  return out;
}
"""


async def _try_open_location_filter(page: Page) -> None:
    for text in ("Filter", "Locations", "Location", "Show results", "Region"):
        try:
            loc = page.get_by_text(text, exact=False)
            if await loc.count() > 0:
                await loc.first.click(timeout=2000)
                await page.wait_for_timeout(500)
        except Exception:
            continue


async def _extract_dom_location_options(
    page: Page,
    country_triple: str,
    add_entry: Callable[[LocationEntry], None],
) -> None:
    """Parse location filter UI for state/region options under the country."""
    country_name, _, _ = parse_location_triple(country_triple)
    if not country_name:
        country_name = country_triple.split(",")[0]

    try:
        options = await page.evaluate(
            """
            () => {
                const out = [];
                const selectors = [
                    '[role="option"]',
                    '[role="menuitem"]',
                    'label',
                    'li',
                    'button',
                ];
                for (const sel of selectors) {
                    for (const el of document.querySelectorAll(sel)) {
                        const text = (el.innerText || el.textContent || '').trim();
                        if (text && text.length < 80) out.push(text);
                    }
                }
                return [...new Set(out)];
            }
            """
        )
    except Exception:
        return

    for label in options or []:
        if not label or label.lower() in {"filter", "locations", "location", "show results"}:
            continue
        if country_name.lower() in label.lower() and label.lower() != country_name.lower():
            continue
        triple = triple_from_parts(country_name, label, None)
        if triple == country_triple:
            continue
        add_entry(
            LocationEntry(
                triple=triple,
                label=label_from_parts(country_name, label, None),
                level=infer_level(country_name, label, None),
                parent_triple=country_triple,
            )
        )


async def _scroll_to_search_pagination(page: Page) -> None:
    try:
        next_btn = page.locator('[aria-label="Go to next page"]')
        if await next_btn.count() > 0:
            await next_btn.first.scroll_into_view_if_needed(timeout=3000)
            return
    except Exception:
        pass
    try:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    except Exception:
        pass


async def _current_pagination_page(page: Page) -> int | None:
    try:
        value = await page.evaluate(
            """
            () => {
              const el = document.querySelector('[aria-current="page"]');
              if (!el) return null;
              const n = parseInt((el.textContent || '').trim(), 10);
              return Number.isFinite(n) ? n : null;
            }
            """
        )
        return int(value) if value is not None else None
    except Exception:
        return None


async def _wait_for_search_page_turn(
    page: Page,
    previous_keys: set[str],
    page_num: int,
    log_fn: Callable[[str], None] | None = None,
    rate_limit_check: Callable[[], bool] | None = None,
) -> None:
    """Wait until the visible result set changes after pagination."""
    cfg = settings()
    poll_ms = int(cfg.get("pagination_poll_ms", 500))
    max_polls = int(cfg.get("pagination_max_polls", 40))
    settle_ms = int(cfg.get("pagination_settle_ms", 800))

    for _ in range(max_polls):
        if rate_limit_check and rate_limit_check():
            raise BlockedError(
                f"Rate limited during pagination on page {page_num}",
                diagnostics=FetchDiagnostics(block_signals=["rate_limited"]),
            )
        await page.wait_for_timeout(poll_ms)
        current = await extract_companies_from_page(page)
        current_keys = {
            k for c in current if (k := company_dedupe_key(c)) is not None
        }
        if not previous_keys and current_keys:
            return
        if current_keys and current_keys != previous_keys:
            await page.wait_for_timeout(settle_ms)
            return

    message = f"Pagination timeout on page {page_num}: result set did not change"
    if log_fn:
        log_fn(f"WAIT: pagination timeout page {page_num} — keys unchanged")
    raise PageNotReadyError(message)


async def _extract_page_companies_with_retry(
    page: Page,
    page_num: int,
    log_fn: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    companies = await extract_companies_from_page(page)
    if companies or page_num == 1:
        return companies

    poll_ms = int(settings().get("pagination_poll_ms", 500))
    retries = int(settings().get("pagination_empty_retry_polls", 24))
    if log_fn:
        log_fn(
            f"WAIT: page {page_num} empty after turn — "
            f"retrying extract up to {retries} times"
        )
    for _ in range(retries):
        await page.wait_for_timeout(poll_ms)
        companies = await extract_companies_from_page(page)
        if companies:
            return companies
    return companies


async def _paginate_search_results(
    page: Page,
    max_pages: int,
    max_companies: int = 100,
    page_size: int = SEARCH_RESULTS_PAGE_SIZE,
    log_fn: Callable[[str], None] | None = None,
    rate_limit_check: Callable[[], bool] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """
    Walk ensun search pages (max ``max_pages``, default 10 for free tier).

    Stop when a page has 0 results, fewer than ``page_size`` results, no next
    button, ``max_pages`` is reached, or ``max_companies`` (100) is collected.
    Ensun free accounts: 10 pages x 10 cards = 100 companies maximum.
    """
    all_companies: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    pages_fetched = 0
    previous_page_keys: set[str] = set()

    for page_num in range(1, max_pages + 1):
        if page_num > 1:
            try:
                await _wait_for_search_page_turn(
                    page,
                    previous_page_keys,
                    page_num,
                    log_fn=log_fn,
                    rate_limit_check=rate_limit_check,
                )
            except PageNotReadyError:
                if all_companies:
                    if log_fn:
                        log_fn(
                            f"WAIT: pagination stalled on page {page_num} — "
                            f"saving {len(all_companies)} companies from prior pages"
                        )
                    return all_companies, pages_fetched
                raise

        page_companies = await _extract_page_companies_with_retry(
            page, page_num, log_fn=log_fn
        )
        page_keys = {
            k for c in page_companies if (k := company_dedupe_key(c)) is not None
        }

        for company in page_companies:
            key = company_dedupe_key(company)
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            all_companies.append(company)

        if len(all_companies) >= max_companies:
            all_companies = all_companies[:max_companies]
            pages_fetched = page_num
            if log_fn:
                log_fn(
                    f"PAGINATE: reached {max_companies} company cap "
                    f"on page {page_num} (ensun free limit)"
                )
            break

        count = len(page_companies)

        if count == 0:
            if log_fn:
                log_fn(f"PAGINATE: stop before page {page_num} — no cards loaded")
            pages_fetched = max(page_num - 1, 0)
            break

        pages_fetched = page_num

        if count < page_size:
            if log_fn:
                log_fn(
                    f"PAGINATE: stop at page {page_num} — "
                    f"partial page ({count}/{page_size})"
                )
            break
        if page_num >= max_pages:
            if log_fn:
                log_fn(f"PAGINATE: reached max_pages={max_pages}")
            break

        await _scroll_to_search_pagination(page)
        clicked = await _try_next_page(page)
        if not clicked:
            if log_fn:
                log_fn(f"PAGINATE: stop at page {page_num} — no next button")
            break
        previous_page_keys = page_keys
        settle_ms = int(settings().get("pagination_settle_ms", 800))
        await page.wait_for_timeout(settle_ms)

    return all_companies, pages_fetched


async def _try_next_page(page: Page) -> bool:
    selectors = [
        '[aria-label="Go to next page"]',
        'button[aria-label*="next page" i]',
        '[aria-label="Next page"]',
        'button:has-text("Next")',
        'button:has-text("Load more")',
        'a:has-text("Next")',
    ]
    for selector in selectors:
        try:
            el = page.locator(selector)
            if await el.count() == 0:
                continue
            btn = el.first
            if await btn.is_disabled():
                continue
            await btn.scroll_into_view_if_needed(timeout=3000)
            await btn.click(timeout=4000)
            return True
        except Exception:
            continue
    return False


def run_async(coro):
    from src.shutdown import register_asyncio_loop

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    register_asyncio_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        register_asyncio_loop(None)
        loop.close()
