"""
steam.py — client for Steam's public, unauthenticated storefront API. No API key,
no scraping: just storesearch (the results list) and appdetails (per-app
description), both under store.steampowered.com.

Two calls, not one, because storesearch's own response has no description field -
only appdetails does, and that's one HTTP call per app. See config.py's
STEAM_DETAILS_* settings for how that's kept from turning a single search into a
pile of sequential round trips.
"""
import concurrent.futures
import html
import logging

import requests

import config

_logger = logging.getLogger(__name__)

STORESEARCH_URL = "https://store.steampowered.com/api/storesearch/"
APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"


def search(term):
    """The results list: name, icon, appid, price - no description. Raises
    requests.RequestException on failure/timeout; the caller (app.py) turns that
    into a degraded "search is unavailable right now" message rather than a 500,
    same as status-portal's media_search."""
    r = requests.get(STORESEARCH_URL, params={"term": term, "l": "english", "cc": "US"},
                      timeout=config.STEAM_SEARCH_TIMEOUT_SECONDS)
    r.raise_for_status()
    payload = r.json()
    items = payload.get("items") or []
    # Filtered before slicing to the limit, not after - storesearch mixes in
    # bundles/other non-app types, and slicing first could return fewer than
    # STEAM_SEARCH_RESULT_LIMIT real games even when more were available further
    # down the list.
    apps = [item for item in items if item.get("type") == "app" and item.get("id") is not None]
    results = []
    for item in apps[:config.STEAM_SEARCH_RESULT_LIMIT]:
        results.append({
            "appid": item["id"],
            "name": item.get("name") or "",
            "icon_url": item.get("tiny_image") or "",
            "short_description": "",
        })
    return results


def _fetch_appdetails_data(appid, timeout=None):
    """The raw `data` object from one appdetails call, or None on any failure
    (timeout, non-200, unexpected shape, or Steam's own "success": false for a
    delisted/region-locked app)."""
    try:
        r = requests.get(APPDETAILS_URL, params={"appids": appid, "cc": "US", "l": "english"},
                         timeout=timeout or config.STEAM_DETAILS_TIMEOUT_SECONDS)
        r.raise_for_status()
        payload = r.json()
        entry = payload.get(str(appid)) or {}
        if not entry.get("success"):
            return None
        return entry.get("data") or {}
    except (requests.RequestException, ValueError) as e:
        _logger.info("Could not fetch Steam appdetails for app %s: %s", appid, e)
        return None


def _clean_description(text):
    """Steam's short_description is meant to be rendered as HTML - it comes back
    with entities like &quot; left un-decoded, which would otherwise show up
    literally once Jinja's autoescaping re-escapes them for plain-text display.
    html.unescape() only resolves recognized entities and leaves everything else
    untouched, so a stray '<' or '&' still reaches the template as plain text and
    still gets escaped normally there - this never grants Steam's response raw
    HTML privileges on the page."""
    return html.unescape(text) if text else ""


def _fetch_short_description(appid):
    """A missing description must never fail the whole search, so this always
    returns a string - "" on any failure."""
    data = _fetch_appdetails_data(appid)
    return _clean_description((data or {}).get("short_description"))


def fetch_app_summary(appid):
    """Canonical name/icon/description for one app, straight from Steam - used to
    fill in a request record server-side rather than trusting the hidden form
    fields a visitor's browser submits (those started life as this same data, but
    a request row is what the admin reviews and acts on, so it's worth the one
    extra round trip to not just take a client's word for it). Returns None if
    the appid doesn't resolve to a real, currently-listed app."""
    data = _fetch_appdetails_data(appid)
    if data is None:
        return None
    return {
        "appid": appid,
        "name": _clean_description(data.get("name")),
        "icon_url": data.get("header_image") or data.get("capsule_image") or "",
        "short_description": _clean_description(data.get("short_description")),
    }


def enrich_with_descriptions(results):
    """Fills in short_description for each search result, concurrently and with a
    bounded worker pool (config.STEAM_DETAILS_WORKERS) - one slow/unreachable app
    can't hold up the rest of the page, and this never raises: a failed fetch just
    leaves that one result's description blank."""
    if not results:
        return results
    with concurrent.futures.ThreadPoolExecutor(max_workers=config.STEAM_DETAILS_WORKERS) as pool:
        descriptions = list(pool.map(_fetch_short_description, [r["appid"] for r in results]))
    for result, description in zip(results, descriptions):
        result["short_description"] = description
    return results
