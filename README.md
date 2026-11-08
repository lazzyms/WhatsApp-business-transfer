# WhatsApp Business transfer (Android → iPhone)

Move your **WhatsApp Business** chat history from an Android phone to an iPhone, for free, from a Mac.

> **Unofficial.** Not affiliated with or endorsed by WhatsApp or Meta. It rewrites an iPhone's
> WhatsApp Business database and restores a backup onto the phone. Use at your own risk, and keep
> your original Android backup.

## What it does

1. Decrypts your Android `msgstore.db.crypt15` backup (using [wa-crypt-tools](https://github.com/ElDavoo/wa-crypt-tools)).
2. Converts the chats into the iPhone WhatsApp Business database format.
3. Checks the result with WhatsApp's own CoreData model (needs Xcode command line tools).
4. Injects it into a local, unencrypted iPhone backup and restores that to the phone with `idevicebackup2`.

## Scope and limits

- **WhatsApp Business only.** Regular WhatsApp is deliberately not touched.
- Tested on macOS only, with one real migration. Text messages work.
- Media is **experimental**: images and files appear as text placeholders such as `<image not transferred>`.
- Group chats are converted but lightly tested.
- Not supported: iPhone → Android.

## Before you start

- Install **WhatsApp Business** on the iPhone and register it with the same number. Do not restore from iCloud.
- On Android, create an end-to-end encrypted backup and keep the 64-digit key.
- On the iPhone, turn **Find My iPhone off** (iOS refuses restores otherwise) and use an **unencrypted** local backup.

## Install

```bash
brew install libimobiledevice
brew install --cask android-platform-tools
xcode-select --install   # optional, enables the safety validation
pipx install git+https://github.com/lazzyms/WhatsApp-business-transfer
```

## Use

```bash
wa-migrate            # guided: pull backup, convert, validate, restore
wa-migrate --no-restore   # stop before touching the phone
```

The restore step only runs after you type `RESTORE`.

If WhatsApp shows **"Recover chat history"**, do not tap *Begin recovery*; open an issue instead.

## Privacy

Everything runs locally. Never commit or share backups, key files, or `ChatStorage.sqlite`.

## Develop

```bash
pip install -e '.[test]'
pytest
```

Tests use synthetic data only.

## License

GPL-3.0-or-later. Decryption relies on wa-crypt-tools (GPLv3+), run as a separate program.
