#!/bin/zsh
# Double-click this file in Finder to start the dashboard and scheduler.
cd "$(dirname "$0")"
exec /usr/bin/env python3 local_app.py
