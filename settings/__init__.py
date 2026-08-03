"""
This folder contains the configuration in different files. This init file imports all files in the current
directory to combine configurations into one settings module.

base.py     -> non-secret defaults, safe to commit.
apps.py     -> list of apps/plugins to load, including our own BetPluggin app.
local.py    -> reads your dedicated server credentials (host/port/user/password) and your login from
               environment variables. Set the real values in a ".env" file (see .env.example), not here.
"""

from .base import *
from .apps import *

try:
	from .local import *
except ImportError:
	pass
