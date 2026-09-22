"""PyInstaller entry: the stock Ryn node daemon, frozen self-contained.

This is the unmodified rynmesh peer (`rynmesh.peer_http:main`); freezing only
removes the system-Python/rynmesh install requirement. Behavior is identical.
"""
import sys
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    if len(sys.argv) > 1 and sys.argv[1] == "--restore-space":
        sys.argv.pop(1)
        from rynmesh.personal_space import main as restore_main
        restore_main()
        sys.exit(0)
    from rynmesh.peer_http import main
    sys.exit(main())
