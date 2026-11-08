"""Shared HTTPS configuration for the assessment website and CRM API."""

from __future__ import annotations

import ssl

import certifi


def trusted_ssl_context() -> ssl.SSLContext:
    """Use certifi's maintained CA bundle; never disable certificate verification."""
    return ssl.create_default_context(cafile=certifi.where())
