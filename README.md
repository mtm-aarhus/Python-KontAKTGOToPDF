# Python-KontAKTGOToPDF

Converts a single **GO** document (Aarhus Kommune's case system) to PDF and uploads it to SharePoint, for the **KontAKT** aktindsigt (FOI request) system.

KontAKT triggers this once per document when a caseworker transfers a case's files to SharePoint.

## What it does

For one GO document:

1. Looks up the document's metadata and resolves `.goref` pointer documents to the real document.
2. Produces a PDF:
   - first via **GO's built-in converter** (authoritative; handles e-mails and Office files),
   - falling back to LibreOffice / Pillow (images) / e-mail rendering via the shared [`oomtm`](https://github.com/mtm-aarhus/oomtm) library for anything GO declines.
3. Uploads the PDF to the KontAKT SharePoint site — one file per document, overwriting any previous version.
4. Reports the result (status + SharePoint URL) back to KontAKT.

Files that can't be converted are uploaded **as their original**, so they still land in SharePoint (just not OCR-screenable). Video / audio / unconvertible binaries are skipped.

## SharePoint layout

```
{site}/Delte dokumenter/{kontakt-sag-id} - {sagstitel}/{GO-sagsnummer}/{aktnr} - {doknr} - {titel}.pdf
```

## Input (one document)

| Field | Meaning |
|-------|---------|
| `kontakt_case_id` | KontAKT case id |
| `doc_id` | KontAKT document id (used for the result callback) |
| `source_case_id` | GO case number |
| `dok_id` | GO document id |
| `akt_id` | Act number (zero-padded in the filename) |
| `title` | Document title |
| `case_title` | KontAKT case title (used for the folder name) |

## Output

The PDF (or unconverted original) in SharePoint, plus a callback to KontAKT with the SharePoint URL, file name, size and SHA-256.

## Required configuration

- Constant `GOApiURL` — GO API base URL
- Credential `GOAktApiUser` — GO API user (NTLM)
- Constant `KontAKTSharePoint` — SharePoint site URL (library: *Delte dokumenter*)
- Credential `SharePointCert` — username = certificate thumbprint, password = certificate path
- Credential `SharePointAPI` — username = tenant, password = client id
- Credential `KontAKTAPI` — username = base URL, password = API key

## Dependencies

The shared [`oomtm`](https://github.com/mtm-aarhus/oomtm) library (`go`, `pdf`, `sharepoint`). PDF conversion auto-installs LibreOffice on the worker if it's missing (no admin required).
