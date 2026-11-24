"""User-imported project follow-up strategy cards.

A card is how to probe, not a question: the import validates only the declarative shape, and the
session freezes the cards it started with so an upgrade never rewrites history.
"""
import re
from typing import Annotated

from pydantic import BaseModel, Field

MAX_CARDS = 12
MAX_TEXT = 4000
SLUG = re.compile(r'^[a-z0-9][a-z0-9-]{0,59}$')
DIMENSIONS = ('contribution', 'verification', 'failure', 'tradeoff')


class StrategyImportRequest(BaseModel):
    document: dict


class StrategyCommit(BaseModel):
    enabled: bool = True


class StrategySettings(BaseModel):
    enabled: bool = True
    version: int | None = Field(default=None, ge=1)
