"""
league_memory._helpers
~~~~~~~~~~~~~~~~~~~~~~
Shared private utility functions used across crud.py and queries.py.

All functions here are pure transformations or tiny SQLite helpers that do
NOT open new connections — they operate on data already in memory or on a
caller-provided `conn` argument.
"""
from __future__ import annotations

import re
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any
