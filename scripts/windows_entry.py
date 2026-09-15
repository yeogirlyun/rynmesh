"""Frozen launcher entry; keep multiprocessing dispatch before app imports."""
import sys
from multiprocessing import freeze_support

if __name__ == '__main__':
    freeze_support()
    try:
        from rynmesh.windows_app import main
        sys.exit(main())
    except Exception:
        from tkinter import messagebox
        messagebox.showerror('Ryn could not start', 'Ryn could not start on this device. Close another Ryn window and try again. Your data and saved invitation are kept.')
        sys.exit(1)
