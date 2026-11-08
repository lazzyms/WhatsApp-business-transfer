"""Safety net: open the converted database with the app's own CoreData model (macOS only).

WhatsApp stores its chats in a CoreData store whose scalar attributes are non-optional.
A single NULL in one of them makes the app report "trouble reading your chat history".
The model WhatsApp itself uses is embedded in every ChatStorage.sqlite (Z_MODELCACHE), so
we load it with Apple's CoreData on the Mac, read every row and run validateForUpdate on
each one. Needs `swiftc` (Xcode command line tools: `xcode-select --install`).
"""
from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

SWIFT_SRC = Path(__file__).with_name("cdvalidate.swift")


class ValidationError(Exception):
    pass


def available() -> bool:
    return sys.platform == "darwin" and shutil.which("swiftc") is not None


def extract_model(app_db: Path, out: Path) -> None:
    """Write the app's compiled CoreData model (an NSKeyedArchiver plist) to `out`."""
    with sqlite3.connect(f"file:{app_db}?mode=ro", uri=True) as db:
        row = db.execute("SELECT Z_CONTENT FROM Z_MODELCACHE").fetchone()
    if not row:
        raise ValidationError("Template database has no Z_MODELCACHE; cannot load the app's model.")
    out.write_bytes(zlib.decompress(row[0], -15))  # raw deflate


def validate(converted: Path, template: Path) -> str:
    """Return the validator's report. Raises ValidationError if any row is invalid."""
    if not available():
        raise ValidationError("swiftc not found (macOS + Xcode command line tools required).")
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        model, exe, copy = tmp / "model.bin", tmp / "cdvalidate", tmp / "ChatStorage.sqlite"
        extract_model(template, model)
        build = subprocess.run(["swiftc", "-O", str(SWIFT_SRC), "-o", str(exe)],
                               capture_output=True, text=True)
        if build.returncode:
            raise ValidationError(f"Could not compile the validator:\n{build.stderr.strip()}")
        # Validate a copy so opening it never leaves -wal/-shm files next to the real one.
        with sqlite3.connect(f"file:{converted}?mode=ro", uri=True) as src, sqlite3.connect(copy) as dst:
            src.backup(dst)
        run = subprocess.run([str(exe), str(model), str(copy)], capture_output=True, text=True)
        if run.returncode:
            detail = "\n".join(run.stderr.strip().splitlines()[:5])
            raise ValidationError("The converted database is NOT valid for WhatsApp:\n"
                                  f"{run.stdout.strip()}\n{detail}".rstrip())
        return run.stdout
