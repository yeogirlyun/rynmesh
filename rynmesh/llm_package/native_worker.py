"""macOS supervisor: stop the engine if the owning Ryn node exits unexpectedly."""
import os
import subprocess
import sys
import time


def main(args):
    parent = int(args[0])
    process = subprocess.Popen(args[1:], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        while process.poll() is None:
            if os.getppid() != parent:
                break
            time.sleep(0.5)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


if __name__ == "__main__":
    main(sys.argv[1:])
