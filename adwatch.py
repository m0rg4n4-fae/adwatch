#!/usr/bin/env python3
"""Run adwatch straight from this folder, no install needed:  python adwatch.py

Your games and saved buttons are then kept in this folder (profiles/ and templates/).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from adwatch.cli import main  # noqa: E402  (the adwatch/ folder next to this file)

if __name__ == "__main__":
    main()
