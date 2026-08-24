"""Gunicorn defaults for hosting panels that force `gunicorn run:app`."""

from __future__ import annotations

import os


_port = os.getenv("PORT", "").strip() or "80"
bind = f"0.0.0.0:{_port}"
worker_class = "uvicorn.workers.UvicornWorker"
workers = 1
timeout = 180
graceful_timeout = 30
accesslog = "-"
errorlog = "-"
capture_output = True
