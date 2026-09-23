from __future__ import annotations

import sys
import types

import pytest

import web_search_tools


class _FakeDDGS:
    calls = []
    behavior = None

    def __init__(self, timeout=12):
        self.timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def text(self, **kwargs):
        type(self).calls.append(dict(kwargs))
        return type(self).behavior(kwargs)


def _install_fake_ddgs(monkeypatch, behavior):
    _FakeDDGS.calls = []
    _FakeDDGS.behavior = staticmethod(behavior)
    module = types.ModuleType("ddgs")
    module.DDGS = _FakeDDGS
    monkeypatch.setitem(sys.modules, "ddgs", module)


def test_auto_mode_uses_ddgs_auto_first(monkeypatch):
    def behavior(kwargs):
        return [
            {
                "title": "result",
                "href": "https://example.com/a",
                "body": "snippet",
            }
        ]

    _install_fake_ddgs(monkeypatch, behavior)

    results = web_search_tools.search_web("test query")

    assert len(results) == 1
    assert _FakeDDGS.calls[0]["backend"] == "auto"
    assert _FakeDDGS.calls[0]["region"] == "cn-zh"


def test_auto_failure_falls_back_to_explicit_provider_group(monkeypatch):
    def behavior(kwargs):
        if kwargs["backend"] == "auto":
            raise TimeoutError("auto timed out")
        return [
            {
                "title": "fallback",
                "href": "https://example.com/fallback",
                "body": "ok",
            }
        ]

    _install_fake_ddgs(monkeypatch, behavior)

    results = web_search_tools.search_web("test query")

    assert results[0]["url"] == "https://example.com/fallback"
    assert _FakeDDGS.calls[1]["region"] == "cn-zh"
    assert "mojeek" in _FakeDDGS.calls[1]["backend"]
    assert "yandex" in _FakeDDGS.calls[1]["backend"]


def test_region_fallback_is_used_after_requested_region_fails(monkeypatch):
    def behavior(kwargs):
        if kwargs["region"] == "cn-zh":
            raise TimeoutError("cn region unavailable")
        if kwargs["region"] == "us-en" and kwargs["backend"] == "auto":
            return [
                {
                    "title": "region fallback",
                    "href": "https://example.com/us",
                    "body": "ok",
                }
            ]
        raise AssertionError("unexpected strategy")

    _install_fake_ddgs(monkeypatch, behavior)

    results = web_search_tools.search_web("中文查询")

    assert results[0]["url"] == "https://example.com/us"
    assert [call["region"] for call in _FakeDDGS.calls] == [
        "cn-zh",
        "cn-zh",
        "us-en",
    ]


def test_all_provider_region_failures_raise_instead_of_fake_success(monkeypatch):
    def behavior(kwargs):
        raise TimeoutError("network unavailable")

    _install_fake_ddgs(monkeypatch, behavior)

    with pytest.raises(RuntimeError) as exc_info:
        web_search_tools.search_web("test query")

    message = str(exc_info.value)
    assert "provider/region strategies" in message
    assert "network unavailable" in message
    assert len(_FakeDDGS.calls) == 4


def test_search_results_are_deduplicated_by_url(monkeypatch):
    def behavior(kwargs):
        return [
            {
                "title": "A1",
                "href": "https://example.com/a",
                "body": "first",
            },
            {
                "title": "A2",
                "href": "https://example.com/a",
                "body": "duplicate",
            },
            {
                "title": "B",
                "href": "https://example.com/b",
                "body": "second",
            },
        ]

    _install_fake_ddgs(monkeypatch, behavior)

    results = web_search_tools.search_web("test query", max_results=10)

    assert [item["url"] for item in results] == [
        "https://example.com/a",
        "https://example.com/b",
    ]


def test_explicit_backend_is_respected_and_can_use_region_fallback(monkeypatch):
    def behavior(kwargs):
        assert kwargs["backend"] == "bing"
        if kwargs["region"] == "cn-zh":
            return []
        return [
            {
                "title": "bing",
                "href": "https://example.com/bing",
                "body": "ok",
            }
        ]

    _install_fake_ddgs(monkeypatch, behavior)

    results = web_search_tools.search_web(
        "test query",
        backend="bing",
    )

    assert results[0]["title"] == "bing"
    assert [call["region"] for call in _FakeDDGS.calls] == ["cn-zh", "us-en"]
