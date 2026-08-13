"""PyInstaller entry: the stock Ryn node daemon, frozen self-contained.

This is the unmodified rynmesh peer (`rynmesh.peer_http:main`); freezing only
removes the system-Python/rynmesh install requirement. Behavior is identical.
"""
import sys

from rynmesh.peer_http import main

if __name__ == "__main__":
    sys.exit(main())
