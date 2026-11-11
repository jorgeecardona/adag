"""Tests for the Config wrapper."""

from adag.config import Config


def test_config_wraps_value():
    """Config stores and returns its value."""
    assert Config(42).value == 42


def test_config_equality():
    """Two Configs with the same value are equal."""
    assert Config(10) == Config(10)


def test_config_inequality():
    """Two Configs with different values are not equal."""
    assert Config(10) != Config(20)


def test_config_repr():
    """Config repr shows the wrapped value."""
    assert repr(Config("hello")) == "Config('hello')"


def test_config_hash_matches_value():
    """Config hashes by its value, so identical values hash the same."""
    assert hash(Config(42)) == hash(42)
