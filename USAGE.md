# A map of your archive

## Browse and search

All Files starts at This PC. Click to enter a folder, double-click to open a connected file in its default app, and use the information button for its inspector. Search across catalogs with partial names and extension patterns. Offline entries remain searchable; opening an original requires its drive.

## Drives and updates

Drive identity uses available volume and hardware information rather than a drive letter alone. Capacity and connection details are shown when available. Offline capacity is a previous observation. Global combines catalogs; it is not a physical disk or a total of free space.

Scan and Update save dates and changes. Updates keeps a local history and can schedule scans while the app is running. The scheduler respects paused scans and does not install a Windows service.

## Projects and memories

These views bring together project folders and personal collections across drives. Same-name grouping helps you explore locations; it does not prove that their contents are identical. Memories keep year and month context so unrelated occasions are not merged just because their names match.

Choose thumbnails, details, compact view, or denser grids. Existing thumbnails stay available offline; generating new ones needs the originals. Video thumbnails require FFmpeg.

## Read limits and cleanup

Scanning, hashing, and thumbnail generation share conservative limits per physical drive and across the app. This controls concurrent reads, not a measured transfer speed. Cached thumbnails and catalog queries do not reread originals.

Duplicate cleanup verifies full SHA-256 hashes and asks you to review and confirm. Confirmed copies go to the Windows Recycle Bin, with no permanent-delete fallback. Files still take space until that bin is emptied. Projects using removed paths may need relinking. Incomplete scans keep earlier records when a file's absence cannot be confirmed.

## Local data and backups

SQLite catalogs, manifests, logs, thumbnails, and backups live in `data/`. A JSON snapshot is written after a completed update. It contains catalog information, not original files or earlier versions of their contents. SQLite powers search and catalog imports.

Settings can create a consistent SQLite backup while the app is running. Keep it on another drive. Import merges catalog records. For an exact rollback, stop the app, preserve the current data folder, then replace the database with the chosen backup. Never remove SQLite sidecar files while the app is running.
