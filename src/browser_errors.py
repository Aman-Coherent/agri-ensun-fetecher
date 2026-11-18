from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FetchDiagnostics:
    page_status: int | None = None
    graphql_statuses: list[int] = field(default_factory=list)
    graphql_ok_count: int = 0
    graphql_error_count: int = 0
    logo_count: int = 0
    heading_count: int = 0
    block_signals: list[str] = field(default_factory=list)
    page_title: str = ""

    @property
    def graphql_count(self) -> int:
        return len(self.graphql_statuses)

    def summary(self) -> str:
        return (
            f"page_status={self.page_status} logos={self.logo_count} "
            f"heading={self.heading_count} graphql_ok={self.graphql_ok_count} "
            f"signals={','.join(self.block_signals) or 'none'}"
        )


class BrowserFetchError(Exception):
    """Base error for browser fetch failures."""

    def __init__(self, message: str, diagnostics: FetchDiagnostics | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics


class BlockedError(BrowserFetchError):
    """HTTP block, rate limit, or bot-detection page."""


class PageNotReadyError(BrowserFetchError):
    """Page or pagination did not become ready in time."""


class CompanyNotFoundError(BrowserFetchError):
    """Company page returned 404/410 — the URL will never exist; do not retry."""


class GraphQLError(BrowserFetchError):
    """GraphQL request failed with a non-recoverable error."""


class VpnServerUnreachableError(Exception):
    """The current VPN server cannot reach ensun.io at all (connection reset,
    DNS failure, page never loads). Reloading on the same server is pointless —
    the only cure is another VPN server."""


def is_browser_driver_error(exc: BaseException) -> bool:
    """True when Playwright's browser/driver process died (context reset is not enough)."""
    name = type(exc).__name__.lower()
    if "targetclosed" in name or "browserclosed" in name:
        return True
    msg = str(exc).lower()
    markers = (
        "connection closed while reading from the driver",
        "target page, context or browser has been closed",
        "browser has been closed",
        "playwright connection closed",
        "pipe closed by peer",
        "socket.send() raised exception",
        "page crashed",
        "page.crash",
    )
    return any(marker in msg for marker in markers)


SEARCH_HEADING_PATTERN = (
    r"companies for|manufacturers for|providers for|"
    r"suppliers for|results for|services for"
)


def is_rate_limited(
    *,
    page_status: int | None = None,
    graphql_statuses: list[int] | None = None,
    graphql_errors: list[int] | None = None,
    rate_limit_codes: list[int] | None = None,
) -> bool:
    if rate_limit_codes is None:
        rate_limit_codes = [429]
    if page_status is not None and page_status in rate_limit_codes:
        return True
    for status in graphql_statuses or []:
        if status in rate_limit_codes:
            return True
    for status in graphql_errors or []:
        if status in rate_limit_codes:
            return True
    return False


def is_search_page_ready(
    *,
    page_status: int | None,
    logo_count: int,
    heading_count: int,
    graphql_ok_count: int,
    min_logos: int = 3,
    min_graphql: int = 1,
    empty_page_http_codes: list[int] | None = None,
    block_http_codes: list[int] | None = None,
) -> bool:
    """True when the search page has loaded enough to scrape or is verified empty."""
    if block_http_codes is None:
        block_http_codes = [403, 429, 502, 503]
    if is_rate_limited(page_status=page_status, rate_limit_codes=block_http_codes):
        return False
    if page_status is not None and page_status in block_http_codes:
        return False
    if empty_page_http_codes is None:
        empty_page_http_codes = [404]
    if page_status is not None and page_status in empty_page_http_codes:
        return True
    if logo_count >= min_logos:
        return True
    if logo_count >= 1 and graphql_ok_count >= min_graphql:
        return True
    if logo_count == 0 and graphql_ok_count >= min_graphql:
        return True
    if logo_count == 0 and heading_count >= 1 and graphql_ok_count >= min_graphql:
        return True
    return False


def is_verified_empty(
    diagnostics: FetchDiagnostics,
    companies_count: int,
    block_http_codes: list[int] | None = None,
    empty_page_http_codes: list[int] | None = None,
) -> bool:
    """True when zero results look like a legitimate empty search, not a failed load."""
    if companies_count > 0:
        return False
    if diagnostics.block_signals:
        return False
    if diagnostics.page_status is not None and block_http_codes:
        if diagnostics.page_status in block_http_codes:
            return False
    if diagnostics.graphql_error_count > 0 and diagnostics.graphql_ok_count == 0:
        return False
    if empty_page_http_codes is None:
        empty_page_http_codes = [404]
    if (
        diagnostics.page_status is not None
        and diagnostics.page_status in empty_page_http_codes
    ):
        return True
    return diagnostics.logo_count == 0 and diagnostics.graphql_ok_count >= 1


def detect_block_signals(
    page_title: str,
    body_text: str,
    patterns: list[str],
) -> list[str]:
    haystack = f"{page_title}\n{body_text}".lower()
    signals: list[str] = []
    for pattern in patterns:
        if pattern.lower() in haystack:
            signals.append(f"body_{pattern.lower().replace(' ', '_')}")
    if "403" in page_title or "403" in body_text[:500]:
        signals.append("http_403_text")
    return signals


def filter_false_positive_block_signals(
    signals: list[str],
    *,
    page_status: int | None,
    logo_count: int,
    heading_count: int,
    graphql_ok_count: int,
    min_logos: int = 3,
) -> list[str]:
    """
    Drop body-text-only block hints when the page clearly loaded search results.

    Ensun result pages often contain the word "challenge" in company text, which
    must not trigger a bot-detection retry loop.
    """
    if not signals:
        return signals
    if any(not s.startswith("body_") and s != "http_403_text" for s in signals):
        return signals
    if page_status != 200:
        return signals
    if logo_count >= min_logos and graphql_ok_count >= 1:
        return []
    if logo_count >= 1 and heading_count >= 1 and graphql_ok_count >= 1:
        return []
    if logo_count >= 1 and graphql_ok_count >= 2:
        return []
    return signals


def filter_false_positive_profile_block_signals(
    signals: list[str],
    *,
    page_status: int | None,
    heading_count: int,
) -> list[str]:
    """
    Drop low-confidence, text-scanned block hints (bare "403" digits, generic
    body-text patterns) on profile pages that already rendered a real company
    heading.

    Profile pages have no "logo count" the way search pages do, so
    filter_false_positive_block_signals above can never clear anything for
    them unless a GraphQL call also happened to succeed on that exact
    navigation — which is not guaranteed (e.g. HTTP-first fetches, cached
    responses). Ensun profile pages routinely contain the literal digits
    "403" in addresses, phone numbers, postal codes or founding years, which
    used to permanently misclassify a perfectly good, fully-rendered profile
    as blocked and retry it forever. A real <h1> with a 200 status is the
    strongest positive signal available that the page actually loaded.
    """
    if not signals or page_status != 200 or heading_count < 1:
        return signals
    if any(not s.startswith("body_") and s != "http_403_text" for s in signals):
        return signals
    return []


BOT_BODY_SIGNALS = frozenset(
    {
        "body_captcha",
        "body_verify_you_are_human",
        "body_checking_your_browser",
        "body_cf-browser-verification",
        "body_hcaptcha",
        "body_recaptcha",
        "body_forbidden",
        "body_access_denied",
    }
)


def is_real_bot_block(diagnostics: FetchDiagnostics) -> bool:
    """True when block signals look like a real bot wall, not a loaded results page."""
    if not diagnostics.block_signals:
        return False
    if not any(s in BOT_BODY_SIGNALS for s in diagnostics.block_signals):
        return False
    if diagnostics.page_status == 200 and diagnostics.logo_count >= 1:
        return False
    if diagnostics.page_status == 200 and diagnostics.graphql_ok_count >= 2:
        return False
    return True
