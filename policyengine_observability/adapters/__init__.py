"""Framework adapters for policyengine-observability."""

from .fastapi import instrument_fastapi
from .flask import instrument_flask

__all__ = ["instrument_fastapi", "instrument_flask"]
