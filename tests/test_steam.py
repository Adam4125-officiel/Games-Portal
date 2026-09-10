import requests

import steam


class _FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self._json


def test_search_filters_to_apps_and_maps_fields(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        assert url == steam.STORESEARCH_URL
        return _FakeResponse({"items": [
            {"type": "app", "id": 70, "name": "Half-Life", "tiny_image": "http://img/70.jpg"},
            {"type": "bundle", "id": 99, "name": "Some Bundle"},
        ]})

    monkeypatch.setattr(steam.requests, "get", fake_get)
    results = steam.search("half-life")
    assert len(results) == 1
    assert results[0] == {"appid": 70, "name": "Half-Life", "icon_url": "http://img/70.jpg",
                           "short_description": ""}


def test_search_respects_result_limit(monkeypatch):
    items = [{"type": "app", "id": i, "name": f"Game {i}", "tiny_image": ""} for i in range(1, 21)]

    def fake_get(url, params=None, timeout=None):
        return _FakeResponse({"items": items})

    monkeypatch.setattr(steam.requests, "get", fake_get)
    monkeypatch.setattr(steam.config, "STEAM_SEARCH_RESULT_LIMIT", 5)
    results = steam.search("game")
    assert len(results) == 5


def test_search_filters_before_slicing_to_the_limit(monkeypatch):
    """Non-app items ahead of real apps in storesearch's own ranking must not
    crowd out real results within the configured limit."""
    items = [{"type": "bundle", "id": 1, "name": "Bundle"}] * 8
    items += [{"type": "app", "id": 70, "name": "Half-Life", "tiny_image": ""}]

    def fake_get(url, params=None, timeout=None):
        return _FakeResponse({"items": items})

    monkeypatch.setattr(steam.requests, "get", fake_get)
    monkeypatch.setattr(steam.config, "STEAM_SEARCH_RESULT_LIMIT", 5)
    results = steam.search("half-life")
    assert len(results) == 1
    assert results[0]["name"] == "Half-Life"


def test_search_raises_on_http_error(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        return _FakeResponse({}, status_code=500)

    monkeypatch.setattr(steam.requests, "get", fake_get)
    try:
        steam.search("anything")
        assert False, "expected an exception"
    except requests.RequestException:
        pass


def test_enrich_with_descriptions_fills_in_short_description(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        appid = params["appids"]
        return _FakeResponse({str(appid): {"success": True, "data": {"short_description": f"desc {appid}"}}})

    monkeypatch.setattr(steam.requests, "get", fake_get)
    results = [{"appid": 70, "name": "Half-Life", "icon_url": "", "short_description": ""}]
    steam.enrich_with_descriptions(results)
    assert results[0]["short_description"] == "desc 70"


def test_enrich_with_descriptions_degrades_gracefully_on_failure(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        raise requests.Timeout("slow")

    monkeypatch.setattr(steam.requests, "get", fake_get)
    results = [{"appid": 70, "name": "Half-Life", "icon_url": "", "short_description": ""}]
    steam.enrich_with_descriptions(results)
    assert results[0]["short_description"] == ""


def test_fetch_app_summary_returns_none_for_unsuccessful_lookup(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        return _FakeResponse({str(params["appids"]): {"success": False}})

    monkeypatch.setattr(steam.requests, "get", fake_get)
    assert steam.fetch_app_summary(999999) is None


def test_fetch_app_summary_returns_canonical_fields(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        return _FakeResponse({str(params["appids"]): {
            "success": True,
            "data": {"name": "Half-Life", "header_image": "http://img/header.jpg",
                      "short_description": "A classic."},
        }})

    monkeypatch.setattr(steam.requests, "get", fake_get)
    summary = steam.fetch_app_summary(70)
    assert summary == {"appid": 70, "name": "Half-Life", "icon_url": "http://img/header.jpg",
                        "short_description": "A classic."}
