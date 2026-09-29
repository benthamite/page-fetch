"""Fetch web pages for automations, including paywalled ones, safely."""

from .routes import ROUTES, FetchFailed, Fetched

__all__ = ["ROUTES", "FetchFailed", "Fetched"]
__version__ = "0.1.0"
