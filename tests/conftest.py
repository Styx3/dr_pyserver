"""Pytest bootstrap shared by the whole suite.

Puts the repo root (the server directory) on ``sys.path`` so ``import drserver``
works regardless of where pytest is invoked, and so test modules can ``import
_paths`` for the single source of truth on filesystem locations.
"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
