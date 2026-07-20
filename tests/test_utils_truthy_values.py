"""Tests for shared environment helpers."""

import os

from utils import env_var_enabled, is_truthy_value, normalize_proxy_env_vars


def test_is_truthy_value_accepts_common_truthy_strings():
    assert is_truthy_value("true") is True
    assert is_truthy_value(" YES ") is True
    assert is_truthy_value("on") is True
    assert is_truthy_value("1") is True


def test_is_truthy_value_respects_default_for_none():
    assert is_truthy_value(None, default=True) is True
    assert is_truthy_value(None, default=False) is False


def test_is_truthy_value_rejects_falsey_strings():
    assert is_truthy_value("false") is False
    assert is_truthy_value("0") is False
    assert is_truthy_value("off") is False


def test_env_var_enabled_uses_shared_truthy_rules(monkeypatch):
    monkeypatch.setenv("HERMES_TEST_BOOL", "YeS")
    assert env_var_enabled("HERMES_TEST_BOOL") is True
    monkeypatch.setenv("HERMES_TEST_BOOL", "no")
    assert env_var_enabled("HERMES_TEST_BOOL") is False


def test_normalize_proxy_env_vars_makes_ipv6_cidr_httpx_safe(monkeypatch):
    value = "127.0.0.1,localhost,::1,127.0.0.0/8,::1/128"
    monkeypatch.setenv("NO_PROXY", value)
    monkeypatch.setenv("no_proxy", value)

    normalize_proxy_env_vars()

    expected = "127.0.0.1,localhost,::1,127.0.0.0/8,http://[::1]/128"
    assert os.environ["NO_PROXY"] == expected
    assert os.environ["no_proxy"] == expected
