#!/usr/bin/env python3
"""Entrypoint wrapper - preserves `python webmap.py <domain> ...` invocation."""

from webmap.cli import main

if __name__ == "__main__":
    main()
