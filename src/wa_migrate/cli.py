"""Interactive WhatsApp Business migration: Android -> iPhone (macOS).

  wa-migrate [--key HEX64] [--workdir ./work] [--device-path PATH] [--no-restore]

Stages (each reuses its artifact in --workdir if you say so, so a failure never
forces you to start over):  1 pull+decrypt  2 iPhone backup  3 convert+validate
4 inject  5 restore. Nothing on the Android phone is modified. Nothing is written to
the iPhone until the final restore step, which needs you to type RESTORE.
"""
from __future__ import annotations

import argparse
import getpass
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from . import __version__, inject_backup, transform_db, validate

PKG = "com.whatsapp.w4b"
DEFAULT_DEVICE_PATH = (f"/sdcard/Android/media/{PKG}/WhatsApp Business/Databases/"
                       "msgstore.db.crypt15")
HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


INSTALL_HINT = "Install with: brew install libimobiledevice && brew install --cask android-platform-tools"


class Abort(Exception):
    pass


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw)
    except FileNotFoundError:
        raise Abort(f"'{cmd[0]}' not found. {INSTALL_HINT}")
    except subprocess.CalledProcessError as e:
        raise Abort(f"{' '.join(cmd[:2])} failed:\n{(e.stderr or e.stdout).strip()}")


def run_live(cmd: list[str]) -> None:
    """Long-running command with output streamed to the terminal."""
    try:
        if subprocess.run(cmd).returncode:
            raise Abort(f"{' '.join(cmd[:2])} exited with an error.")
    except FileNotFoundError:
        raise Abort(f"'{cmd[0]}' not found. {INSTALL_HINT}")


def confirm(q: str, default: bool = False) -> bool:
    a = input(f"{q} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    return default if not a else a.startswith("y")


def reuse(path: Path, what: str) -> bool:
    return path.exists() and confirm(f"{what} already exists at {path}. Reuse it?", True)


def wadecrypt_bin() -> str:
    venv = Path(sys.executable).parent / "wadecrypt"
    found = str(venv) if venv.exists() else shutil.which("wadecrypt")
    if not found:
        raise Abort("wadecrypt not found. Reinstall wa-migrate (brew reinstall wa-migrate or pipx reinstall wa-migrate)")
    return found


def preflight() -> tuple[str, str]:
    for tool in ("adb", "idevicebackup2", "ideviceinfo"):
        if not shutil.which(tool):
            raise Abort(f"'{tool}' missing. {INSTALL_HINT}")
    wadecrypt_bin()
    adb = run(["adb", "devices"]).stdout.splitlines()[1:]
    ready = [l.split()[0] for l in adb if l.strip().endswith("device")]
    if len(ready) != 1:
        raise Abort(f"Need exactly one authorised Android device, found {len(ready)}. "
                    "Enable USB debugging and accept the prompt on the phone.")
    try:
        udid = run(["ideviceinfo", "-k", "UniqueDeviceID"]).stdout.strip()
    except Abort:
        raise Abort("No iPhone detected. Connect it, unlock it and tap 'Trust'.")
    print(f"  Android: {ready[0]}\n  iPhone:  {udid}")
    return ready[0], udid


def get_key(cli_key: str | None) -> str:
    key = cli_key or os.environ.get("WA_KEY") or getpass.getpass("64-digit hex key (hidden): ")
    key = key.strip().replace(" ", "")
    if not HEX64.match(key):
        raise Abort("Key must be exactly 64 hex digits.")
    return key.lower()


def stage_decrypt(work: Path, cli_key: str | None, device_path: str) -> Path:
    enc, db = work / "msgstore.db.crypt15", work / "msgstore.db"
    if reuse(db, "Decrypted msgstore.db"):
        return db
    if not reuse(enc, "Encrypted backup"):
        listing = run(["adb", "shell", "ls", "-l", f'"{Path(device_path).parent}"']).stdout
        print(listing)
        device_path = input(f"Path on phone [{device_path}]: ").strip() or device_path
        run(["adb", "pull", device_path, str(enc)])
    db.unlink(missing_ok=True)
    key = get_key(cli_key)
    # wadecrypt takes the hex key as an argument (visible to local `ps` while it runs).
    run([wadecrypt_bin(), key, str(enc), str(db)])
    try:
        n = sqlite3.connect(f"file:{db}?mode=ro", uri=True).execute(
            "SELECT count(*) FROM message").fetchone()[0]
    except sqlite3.Error as e:
        db.unlink(missing_ok=True)
        raise Abort(f"Decrypted file is not a valid msgstore (wrong key?): {e}")
    print(f"  Decrypted OK: {n} rows in message table")
    return db


def stage_ios_backup(work: Path, udid: str) -> Path:
    root = work / "ios_backup"
    if root.exists() and confirm(f"iPhone backup exists at {root}. Reuse it?", True):
        return inject_backup.find_backup(root)
    print("\nBefore continuing, on the iPhone: install WhatsApp Business, register the SAME\n"
          "number and finish setup (skip any restore prompt). The backup must contain it.")
    if not confirm("WhatsApp Business is installed and registered on the iPhone?"):
        raise Abort("Do that first, then re-run.")
    root.mkdir(exist_ok=True)
    run_live(["idevicebackup2", "-u", udid, "backup", "--full", str(root)])
    backup = inject_backup.find_backup(root)
    inject_backup.require_unencrypted(backup)
    return backup


def stage_media(work: Path, device_path: str) -> Path | None:
    """Optionally pull Android media. Returns the folder that contains 'Media/'."""
    root = work / "android"
    base = str(Path(device_path).parent.parent)  # .../WhatsApp Business
    if (root / "Media").exists() and confirm(f"Android media already pulled at {root}. Reuse it?", True):
        return root
    size = run(["adb", "shell", "du", "-sh", f'"{base}/Media"']).stdout.split()[0:1]
    print(f"  Media on phone: {size[0] if size else '?'} (needs that much free space here, "
          "and again inside the iPhone backup)")
    if not confirm("Transfer media (photos, videos, voice notes, documents)? EXPERIMENTAL", False):
        return None
    root.mkdir(exist_ok=True)
    run_live(["adb", "pull", f"{base}/Media", str(root)])
    return root


def stage_convert(work: Path, msgstore: Path, backup: Path,
                  media_root: Path | None) -> tuple[Path, list]:
    out = work / DB_OUT
    template = work / "ChatStorage.template.sqlite"
    template.unlink(missing_ok=True)
    domain = inject_backup.extract(backup, template)
    print(f"  Template extracted from {domain}")
    transform_db.inspect(template)
    existing = sqlite3.connect(f"file:{template}?mode=ro", uri=True).execute(
        "SELECT count(*) FROM ZWAMESSAGE").fetchone()[0]
    if existing > 500:
        raise Abort(f"Business database already has {existing} messages; refusing to append.")
    if existing and not confirm(f"WhatsApp Business already has {existing} messages (normally its own "
                                "welcome/system notices). Append the Android chats to them?", True):
        raise Abort("Stopped.")
    out.unlink(missing_ok=True)
    stats = transform_db.convert(msgstore, template, out,
                                 allow_nonempty=bool(existing), media_root=media_root)
    print(f"  Converted {stats['messages']}/{stats['android_total']} messages, "
          f"{stats['chats']} chats ({stats['skipped']} skipped), {stats['media']} media files")
    if validate.available():
        print("  Validating with WhatsApp's own CoreData model (takes a few seconds)...")
        try:
            validate.validate(out, template)
        except validate.ValidationError as e:
            raise Abort(str(e))
        print("  Validation passed: every row is readable by the app.")
    else:
        print("  WARNING: skipping validation (needs macOS + `xcode-select --install`). A row the\n"
              "  app cannot read shows 'Recover chat history' after the restore; do NOT tap Begin recovery.")
    return out, stats["media_files"]


DB_OUT = "ChatStorage.sqlite"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key", help="64-digit hex key (or env WA_KEY, or prompted)")
    ap.add_argument("--workdir", type=Path, default=Path("work"))
    ap.add_argument("--device-path", default=DEFAULT_DEVICE_PATH)
    ap.add_argument("--no-restore", action="store_true",
                    help="stop after preparing the backup; restore it yourself later")
    ap.add_argument("--version", action="version", version=f"wa-migrate {__version__}")
    a = ap.parse_args()
    work = a.workdir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    try:
        print("== Checking devices ==")
        _, udid = preflight()
        print("== 1. Pull + decrypt ==")
        msgstore = stage_decrypt(work, a.key, a.device_path)
        print("== 2. iPhone backup ==")
        backup = stage_ios_backup(work, udid)
        print("== 3. Media + convert ==")
        media_root = stage_media(work, a.device_path)
        new_db, media = stage_convert(work, msgstore, backup, media_root)
        print("== 4. Inject ==")
        if confirm("Inject converted database into the local backup?", True):
            saved = inject_backup.inject(backup, new_db, media=media, originals_root=work)
            print(f"  Done. Originals saved in {saved}")
        else:
            raise Abort("Stopped before injecting.")
        print("== 5. Restore ==")
        if a.no_restore:
            print("Stopped as requested. Restore later with:\n"
                  f"  idevicebackup2 -u {udid} restore --system --reboot '{work / 'ios_backup'}'")
            return 0
        print("\n  !! Turn OFF Find My iPhone: Settings > [your name] > Find My > Find My iPhone.\n"
              "  !! The restore overwrites the iPhone with the backup state; keep it connected\n"
              "     and unlocked. Changes made on the phone since the backup will be lost.\n")
        if input("Type RESTORE to proceed (anything else stops): ").strip() != "RESTORE":
            print(f"Stopped. Restore later with:\n  idevicebackup2 restore --system --reboot '{work / 'ios_backup'}'")
            return 0
        run_live(["idevicebackup2", "-u", udid, "restore", "--system", "--reboot",
                  str(work / "ios_backup")])
        print("Restore finished. Open WhatsApp Business on the iPhone and check the chats.")
    except (Abort, transform_db.TransformError, inject_backup.BackupError) as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    return 0
