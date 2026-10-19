# ColdNas Manager — manual batches

- Removed channel synchronization commands, handlers and startup dependencies.
- Added SQLite batch and item persistence in `/app/data/batches.db`.
- Closed batches recover automatically after Docker restart; open batches stay open.
- Completed files in the temporary directory are reused after restart; interrupted downloads restart that file. Byte-level resume is **not** implemented.
- Panel, batch list, file pagination, basic pause/resume, activity and settings.
- Message updates throttled to 30–60 seconds (`TG_PROGRESS_INTERVAL`). Phase changes may update immediately.
- Archive extraction with 7z, CRC testing, deletion of successful archive parts and overwrite behavior.

## Deployment

Keep `/app/data`, `/data/temp`, `/app/session` and NAS destination mounts persistent. Build the image again with `docker compose up -d --build`.

## Limitations / cautions

- Pausing prevents new files from starting; it does not interrupt an in-flight Telethon transfer.
- Recovery resumes completed *files*, not bytes within interrupted files.
- Files with identical names in one batch may overwrite each other. Use unique filenames when submitting.
- Extraction and final NAS moves are not fully transactional. If interrupted mid-extraction or move, they may be retried; retain backups of important destination files.
- Archive multipart detection relies on naming and 7z validation. Archive files not recognized by the grouping logic will be moved without extraction.
- This build has been syntax-checked, but cannot be end-to-end tested against the user's Telegram/NAS here.
