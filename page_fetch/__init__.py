"""Fetch web pages for automations, including paywalled ones, safely."""

from .syndication import republished
from .routes import ROUTES, FetchFailed, Fetched

ROUTES["republished"] = republished

__all__ = ["ROUTES", "FetchFailed", "Fetched", "republished"]
__version__ = "0.2.2"
