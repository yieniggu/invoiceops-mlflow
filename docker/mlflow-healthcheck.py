#!/usr/bin/env python3
"""Treat MLflow's expected anonymous Basic Auth rejection as ready."""

from urllib.error import HTTPError
from urllib.request import urlopen


try:
    urlopen("http://127.0.0.1:5000/", timeout=3)
except HTTPError as error:
    if error.code == 401:
        raise SystemExit(0)

raise SystemExit(1)
