# Python-KontAKTGOToPDF

Converts a single **GO** document (Aarhus Kommune's case system) to PDF and stores it in KontAKT's local file store, for the **KontAKT** aktindsigt (FOI request) system.

KontAKT triggers this once per document when a caseworker transfers a case's files.

## What it does

For one GO document:

1. Looks up the document's metadata and resolves `.goref` pointer documents to the real document.
2. Produces a PDF:
   - first via **GO's built-in converter** (authoritative; handles e-mails and Office files),
   - falling back to LibreOffice / Pillow (images) / e-mail rendering via the shared [`oomtm`](https://github.com/mtm-aarhus/oomtm) library for anything GO declines.
3. POSTs the PDF bytes into KontAKT's local file store (`POST /api/v1/cases/{id}/documents/{doc_id}/store`) — one call does the upload and records name/size/SHA-256/status.

Files that can't be converted are stored **as their original** (still delivered, just not OCR-screenable). Video / audio / unconvertible binaries are skipped.

## Input (one document)

| Field | Meaning |
|-------|---------|
| `kontakt_case_id` | KontAKT case id |
| `doc_id` | KontAKT document id (the store is addressed by this id) |
| `source_case_id` | GO case number |
| `dok_id` | GO document id |
| `akt_id` | Act number (zero-padded in the stored filename) |
| `title` | Document title |
| `case_title` | KontAKT case title |

## Output

The PDF (or unconverted original) written into KontAKT's file store; the `/store` endpoint records the name, size, SHA-256 and status. Errors are reported via the `/file` status callback.

## Required configuration

- Constant `GOApiURL` — GO API base URL
- Credential `GOAktApiUser` — GO API user (NTLM)
- Credential `KontAKTAPI` — username = base URL, password = API key

## Dependencies

The shared [`oomtm`](https://github.com/mtm-aarhus/oomtm) library (`go`, `pdf`). PDF conversion auto-installs LibreOffice on the worker if it's missing (no admin required).
