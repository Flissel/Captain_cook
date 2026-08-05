"""ASGI composition root for the controlled provider container."""

from __future__ import annotations

import os

from portal_provider.app import create_app, load_settings


app = create_app(settings=load_settings(os.environ))

