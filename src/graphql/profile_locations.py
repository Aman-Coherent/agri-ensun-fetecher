"""Extract data from the ensun profile Locations tab (HQ card + map + social links)."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from playwright.async_api import Page

from src.graphql.profile_normalize import parse_countries_operating, parse_labeled_line

_ENSUN_SOCIAL_RE = re.compile(r"linkedin\.com/company/ensun|facebook\.com/.*ensun|twitter\.com/.*ensun", re.I)


def normalize_google_maps_url(url: str | None) -> str | None:
    """Prefer a public Google Maps link over embed iframe URLs."""
    if not url:
        return None
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    place_q = query.get("q", [None])[0]
    if place_q and ("embed" in url or "maps/embed" in url):
        return f"https://www.google.com/maps/search/?api=1&query={quote(unquote(place_q))}"
    if parsed.path.startswith("/maps/place/"):
        return url.split("&")[0]
    return url


def _is_ensun_social(href: str) -> bool:
    return bool(_ENSUN_SOCIAL_RE.search(href))


async def extract_locations_tab(page: Page) -> dict[str, Any]:
    """Scrape Locations tab: main address, website, social links, Google Maps."""
    raw: dict[str, Any] = await page.evaluate(
        """
        () => {
          const isEnsunSite = (u) => /ensun(\\.io|gmbh|[-_]gmbh)/i.test(u || '');
          const isEnsunSocial = (u) => /linkedin\\.com\\/company\\/ensun|facebook\\.com\\/.*ensun|twitter\\.com\\/.*ensun/i.test(u || '');
          const bodyText = document.body.innerText || '';

          const hqHeading = document.querySelector('#locations')
            || Array.from(document.querySelectorAll('h2,h3,h4')).find(
              (el) => (el.innerText || '').startsWith('Headquarter of')
            );

          let hqBlock = null;
          if (hqHeading) {
            let block = hqHeading.closest('[class*="MuiGrid-container"], [class*="MuiBox"], section')
              || hqHeading.parentElement;
            for (let i = 0; i < 20 && block; i++) {
              const t = block.innerText || '';
              if (t.includes('City:') && t.includes('Country:')) {
                hqBlock = block;
                break;
              }
              block = block.parentElement;
            }
          }

          const result = {
            hqText: hqBlock ? (hqBlock.innerText || '').slice(0, 1200) : '',
            bodySnippet: '',
            website_url: null,
            linkedin_url: null,
            facebook_url: null,
            twitter_url: null,
            google_maps_url: null,
            locTableRows: [],
          };

          if (!result.hqText && bodyText.includes('Headquarter of')) {
            const start = bodyText.indexOf('Headquarter of');
            let end = bodyText.indexOf('Locations of', start);
            if (end < 0) end = bodyText.indexOf('Frequently asked', start);
            if (end < 0) end = start + 900;
            result.bodySnippet = bodyText.slice(start, end);
          }

          const classifyLink = (href, txt, aria) => {
            if (!href || isEnsunSite(href)) return;
            if (href.includes('linkedin.com')) {
              if (!isEnsunSocial(href) && !result.linkedin_url) result.linkedin_url = href;
            } else if (href.includes('facebook.com')) {
              if (!isEnsunSocial(href) && !result.facebook_url) result.facebook_url = href;
            } else if (href.includes('twitter.com') || href.includes('x.com/')) {
              if (!isEnsunSocial(href) && !result.twitter_url) result.twitter_url = href;
            } else if (
              href.includes('google.com/maps') ||
              href.includes('maps.google.com') ||
              txt.includes('Open in Maps') ||
              aria.includes('Open in Maps')
            ) {
              if (!result.google_maps_url) result.google_maps_url = href;
            } else if (
              txt === 'Website' ||
              aria.toLowerCase() === 'website' ||
              (txt.toLowerCase().includes('website') && !href.includes('ensun'))
            ) {
              if (!result.website_url) result.website_url = href;
            }
          };

          const scanLinks = (root) => {
            if (!root) return;
            for (const a of root.querySelectorAll('a[href]')) {
              classifyLink(
                a.href,
                (a.innerText || '').trim(),
                (a.getAttribute('aria-label') || '').trim()
              );
            }
            for (const iframe of root.querySelectorAll('iframe[src]')) {
              const src = iframe.src || '';
              if (src.includes('google.com/maps') && !result.google_maps_url) {
                result.google_maps_url = src;
              }
            }
          };

          scanLinks(hqBlock);
          const missingSocial = () =>
            !result.linkedin_url ||
            !result.facebook_url ||
            !result.twitter_url ||
            !result.google_maps_url;
          if (missingSocial()) {
            scanLinks(document.body);
          }

          for (const table of document.querySelectorAll('table')) {
            const header = (table.innerText || '').slice(0, 80);
            if (
              header.includes('Location') &&
              header.includes('Country') &&
              header.includes('City')
            ) {
              for (const tr of table.querySelectorAll('tr')) {
                const cells = Array.from(tr.querySelectorAll('td')).map((c) =>
                  c.innerText.trim()
                );
                if (cells.length >= 4) {
                  result.locTableRows.push({
                    location_role: cells[0],
                    country: cells[1],
                    state: cells[2],
                    city: cells[3],
                  });
                }
              }
            }
          }

          return result;
        }
        """
    )

    hq_text = raw.get("hqText") or raw.get("bodySnippet") or ""
    main_city = parse_labeled_line(hq_text, "City")
    main_state = parse_labeled_line(hq_text, "State")
    main_country = parse_labeled_line(hq_text, "Country")
    countries_operating = parse_countries_operating(hq_text)

    loc_rows = raw.get("locTableRows") or []
    if not main_city and loc_rows:
        hq_row = next(
            (r for r in loc_rows if (r.get("location_role") or "").lower() == "headquarter"),
            loc_rows[0] if loc_rows else None,
        )
        if hq_row:
            main_city = hq_row.get("city")
            main_state = hq_row.get("state")
            main_country = hq_row.get("country")

    maps_url = normalize_google_maps_url(raw.get("google_maps_url"))
    linkedin = raw.get("linkedin_url")
    if linkedin and _is_ensun_social(linkedin):
        linkedin = None

    return {
        "main_city": main_city,
        "main_state": main_state,
        "main_country": main_country,
        "countries_operating_count": countries_operating,
        "website_url": raw.get("website_url"),
        "linkedin_url": linkedin,
        "facebook_url": raw.get("facebook_url"),
        "twitter_url": raw.get("twitter_url"),
        "google_maps_url": maps_url,
        "locations": loc_rows,
    }


def merge_locations_into_profile(profile: dict[str, Any], loc: dict[str, Any]) -> dict[str, Any]:
    """Apply Locations-tab data; HQ fields override earlier About-page guesses."""
    if loc.get("main_city"):
        profile["hq_city"] = loc["main_city"]
        profile["city"] = loc["main_city"]
    if loc.get("main_state"):
        profile["hq_state"] = loc["main_state"]
        profile["state"] = loc["main_state"]
    if loc.get("main_country"):
        profile["hq_country"] = loc["main_country"]
        profile["country"] = loc["main_country"]
    if loc.get("countries_operating_count") is not None:
        profile["countries_operating_count"] = loc["countries_operating_count"]
    if loc.get("website_url"):
        profile["website_url"] = loc["website_url"]
    for key in ("linkedin_url", "facebook_url", "twitter_url", "google_maps_url"):
        if loc.get(key):
            profile[key] = loc[key]
    if loc.get("locations"):
        profile["locations"] = loc["locations"]
    return profile
