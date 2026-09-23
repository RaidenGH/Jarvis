"""Shared test setup: keep tests hermetic.

Blank the history file so lifespan-built SessionStores never write JSON to
disk during test runs (must happen before app.main is first imported).
"""

import os

os.environ["JARVIS_HISTORY_PATH"] = ""
