"""HTTP-first profile fetch with Playwright fallback."""

from __future__ import annotations

from typing import Any

from src.browser_errors import BlockedError, CompanyNotFoundError, FetchDiagnostics, PageNotReadyError
from src.config_loader import settings
from src.graphql.profile_extract import (
    PROFILE_REQUIRED,
    extract_from_graphql_responses,
    merge_profile_data,
)
from src.graphql.profile_normalize import (
    is_valid_profile_description,
    validate_profile_record,
)
from src.profile_html_parse import CheckpointPageError, parse_profile_html_to_dom
from src.profile_http import ProfileHttpClient, ProfileHttpError
from src.session import BrowserSession

# Fallback defaults only — the live set always comes from settings.yaml
# (block_http_codes) so search, profile-HTTP and profile-Playwright agree on
# what counts as a block/rate-limit response (ensun has been seen to return
# 400/402 while rate-limited, not just 403/429/503).
_DEFAULT_BLOCK_HTTP_CODES = frozenset({400, 402, 403, 429, 502, 503})
NOT_FOUND_HTTP_CODES = frozenset({404, 410})


def _block_http_codes() -> frozenset[int]:
    raw = settings().get("block_http_codes") or _DEFAULT_BLOCK_HTTP_CODES
    return frozenset(int(code) for code in raw)


class ProfileFetcher:
    """Try curl_cffi HTTP parse first; fall back to Playwright when incomplete."""

    def __init__(
        self,
        browser: BrowserSession,
        http_client: ProfileHttpClient | None,
        *,
        http_enabled: bool | None = None,
        coordinator: Any | None = None,
    ) -> None:
        self.browser = browser
        self.http_client = http_client
        self.coordinator = coordinator
        cfg = settings()
        self._http_enabled = (
            http_enabled
            if http_enabled is not None
            else bool(cfg.get("profile_http_enabled", True))
        )

    def _http_usable(self) -> bool:
        return self._http_enabled and self.http_client is not None

    @staticmethod
    def _is_recoverable_http_block(exc: BlockedError) -> bool:
        diag = exc.diagnostics
        codes = _block_http_codes()
        if diag and diag.page_status in codes:
            return True
        msg = str(exc)
        return any(str(code) in msg for code in codes)

    async def _recover_http_block(self, exc: BlockedError, attempt: int) -> None:
        max_retries = int(settings().get("fetch_max_retries", 10))
        if self.coordinator is not None:
            await self.coordinator.wait_recovery_done()
            await self.coordinator.wait_rate_limit_cooldown()
        ok = await self.browser.recover_from_block(exc, attempt, max_retries)
        if self.coordinator is not None:
            await self.coordinator.wait_recovery_done()
            await self.coordinator.wait_rate_limit_cooldown()
        if not ok:
            raise exc

    @staticmethod
    def _needs_playwright_fallback(merged: dict[str, Any]) -> list[str]:
        missing = validate_profile_record(merged, PROFILE_REQUIRED)
        if missing:
            return missing

        employee_total = int(merged.get("employee_total_visible") or 0)
        employees = merged.get("employees") or []
        if employee_total > 0 and not employees:
            return ["employees"]

        if merged.get("scrape_status") == "partial":
            return ["scrape_status_partial"]

        has_web = bool(merged.get("website_url"))
        has_social = any(
            merged.get(k)
            for k in ("linkedin_url", "facebook_url", "twitter_url", "google_maps_url")
        )
        if not has_web and not has_social:
            return ["website_or_social"]

        if not is_valid_profile_description(merged.get("description")):
            return ["description"]

        return []

    async def _try_http(
        self,
        company_id: str,
        company_name: str,
        profile_url: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if not self._http_usable():
            return None, None
        assert self.http_client is not None
        if self.coordinator is not None:
            await self.coordinator.wait_recovery_done()
            await self.coordinator.wait_rate_limit_cooldown()

        # If this request gets blocked, recovery must not reuse a VPN switch
        # that finished before it started (that server is the failing one).
        self.browser.note_request_started()
        status, html = await self.http_client.fetch_html(profile_url)
        if status in _block_http_codes():
            raise BlockedError(
                f"HTTP {status} for {profile_url}",
                diagnostics=FetchDiagnostics(
                    page_status=status,
                    block_signals=[f"page_http_{status}"],
                ),
            )
        if status in NOT_FOUND_HTTP_CODES:
            raise CompanyNotFoundError(f"HTTP {status} for {profile_url}")
        if status >= 400 or not html:
            return None, None

        try:
            dom = parse_profile_html_to_dom(
                html,
                company_id=company_id,
                company_name=company_name,
                profile_url=profile_url,
            )
        except CheckpointPageError:
            return None, None

        dom["is_managed"] = dom.get("is_managed", 0)
        dom["is_claimable"] = dom.get("is_claimable", 0)

        merged = merge_profile_data(company_id, profile_url, dom, {})
        merged["company_name"] = merged.get("company_name") or company_name
        merged["data_source"] = "http"
        gaps = self._needs_playwright_fallback(merged)
        if gaps:
            merged["_http_gaps"] = gaps
            return None, dom
        return merged, None

    async def _try_playwright(
        self,
        company_id: str,
        company_name: str,
        profile_url: str,
        *,
        http_dom: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        graphql_responses, dom_data = await self.browser.fetch_profile_page(
            profile_url,
            http_dom=http_dom,
        )
        gql = extract_from_graphql_responses(graphql_responses)
        profile = merge_profile_data(company_id, profile_url, dom_data, gql)
        profile["company_name"] = profile.get("company_name") or company_name
        if http_dom:
            profile["data_source"] = "mixed"
        return profile

    async def fetch_profile(
        self,
        company_id: str,
        company_name: str,
        urls: list[str],
    ) -> tuple[dict[str, Any], str]:
        last_error: Exception | None = None
        max_http_retries = int(settings().get("profile_http_block_retries", 5))

        for url in urls:
            http_dom: dict[str, Any] | None = None
            http_attempt = 0
            http_done = False
            skip_playwright = False

            while self._http_usable() and http_attempt < max_http_retries and not http_done:
                try:
                    profile, partial_dom = await self._try_http(
                        company_id, company_name, url
                    )
                    if profile is not None:
                        return profile, "http"
                    if partial_dom is not None:
                        http_dom = partial_dom
                    http_done = True
                except CompanyNotFoundError:
                    raise
                except BlockedError as exc:
                    http_attempt += 1
                    if (
                        not self._is_recoverable_http_block(exc)
                        or http_attempt >= max_http_retries
                    ):
                        raise
                    await self._recover_http_block(exc, http_attempt)
                except PageNotReadyError as exc:
                    last_error = exc
                    skip_playwright = True
                    break
                except ProfileHttpError as exc:
                    last_error = exc
                    http_done = True
                except Exception as exc:
                    last_error = exc
                    http_done = True

            if skip_playwright:
                continue

            try:
                profile = await self._try_playwright(
                    company_id,
                    company_name,
                    url,
                    http_dom=http_dom,
                )
                source = "playwright" if not http_dom else "mixed"
                return profile, source
            except CompanyNotFoundError:
                raise
            except PageNotReadyError as exc:
                last_error = exc
                continue
            except BlockedError:
                raise
            except Exception as exc:
                last_error = exc

        if last_error:
            raise last_error
        raise RuntimeError("All profile URL variants failed")
