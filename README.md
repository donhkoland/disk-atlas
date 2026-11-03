# Disk Atlas

[Download the latest release](https://github.com/donhkoland/disk-atlas/releases/latest)

**Know where it is. Even when the drive isn't here.**

A local catalog for finding files on disconnected drives. Keep a searchable map of names, locations, projects, and formats without copying the originals.

## Set up on Windows

You'll need Python 3.10 or later. Run `SETUP.bat` once, then `START.bat`. The app opens at `http://127.0.0.1:8765`. Use `STOP.bat` for a safe shutdown.

Or create the environment yourself:

```sh
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe run.py
```

## Build your map

Register a drive or folder, confirm the scan, and review any warnings. Search by name, project, or format. Reconnect the original drive when you need to open a file.

Catalogs and thumbnails are created in `data/` in your own copy. This project ships without device inventories, drive labels, serial numbers, or files from the creator's computer. The app reads volume information on the computer where you run it so it can recognize connected drives. That data stays local.

No accounts, AI service, or added telemetry.

## Know the limits

A catalog isn't a backup. Duplicate cleanup requires review and confirmation and uses the Windows Recycle Bin. Test with disposable files first. Drive detection and desktop actions are built for Windows; other platforms aren't promised to behave the same way.

More detail: [USAGE.md](USAGE.md).

## Checks

With the app dependencies installed, add the FastAPI test client dependency:

```sh
python -m pip install httpx
python -m unittest discover -s tests
```

Tests use temporary folders and made-up catalogs.

---

Part of [Tools](https://github.com/donhkoland/Tools). If it helps you find your way around an archive, **give @donhkoland a shout**. It's optional.
