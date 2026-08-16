"""Tests for the test harness itself.

The suite's promise is that running it never touches YouTube, Pexels or Ollama.
That promise is only worth anything if the guard enforcing it is verified.
"""

from __future__ import annotations

import socket
import urllib.error
import urllib.request

import pytest

from faceless import llm, pexels


def test_the_network_guard_blocks_raw_sockets():
    with pytest.raises(RuntimeError, match="network access attempted"):
        socket.create_connection(("example.com", 80), timeout=1)


def test_the_network_guard_blocks_urllib():
    """urlopen goes through create_connection, so it trips the same wire."""
    with pytest.raises((RuntimeError, urllib.error.URLError)):
        urllib.request.urlopen("http://example.com", timeout=1)


def test_ollama_is_unreachable_from_a_unit_test():
    """A stray llm call must fail loudly rather than quietly load an 8GB model."""
    with pytest.raises((llm.LLMError, RuntimeError)):
        llm.generate_json("hello", {"type": "object"}, timeout=1)


def test_pexels_is_unreachable_from_a_unit_test():
    with pytest.raises((pexels.PexelsError, RuntimeError)):
        pexels.search("hippo", key="whatever")


def test_integration_marker_is_registered(pytestconfig):
    """`--strict-markers` plus this check keeps a typo'd marker from silently
    turning an integration test into one that runs against real services."""
    markers = pytestconfig.getini("markers")
    assert any(marker.startswith("integration:") for marker in markers)
