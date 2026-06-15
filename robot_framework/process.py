"""KontAKT GO → PDF → SharePoint robot.

Queue-driven, one queue element per document. For a single GO document it:
  1. fetches metadata (resolving .goref pointers to the real document),
  2. produces a PDF — GO's built-in converter first (authoritative, handles
     emails + office), falling back to LibreOffice / Pillow / email-render via
     oomtm.pdf for anything GO declines,
  3. uploads the PDF to the KontAKT SharePoint site (one file per document,
     overwriting any previous version),
  4. reports status + the SharePoint URL back to KontAKT.

Videos / audio / unconvertible binaries are skipped (status='skipped') with a
note; they're never uploaded.

The GO session, SharePoint context and cached credentials live on the ``Client``
opened in ``reset.open_all`` and are reused across every queue element (the
framework reconnects via ``reset.reset`` on a retry) — so a 2000-document case
doesn't re-authenticate to SharePoint 2000 times.

SharePoint layout:
    {site}/Delte dokumenter/{case_id} - {case title}/{GO sagsnr}/{akt} - {dok} - {title}.pdf

Queue payload (set by KontAKT's "Hent filer" trigger):
    {
        "kontakt_case_id": 11,
        "doc_id": 42,                       # case_documents.id
        "source_case_id": "GEO-2024-000170",
        "dok_id": "8431876",
        "akt_id": 1,
        "title": "Klage over byggetilladelse",
        "case_title": "Aktindsigt i byggesag"
    }

OO config:
    Constant   GOApiURL
    Credential GOAktApiUser           — NTLM user/pwd for GO
    Constant   KontAKTSharePoint      — SharePoint site URL (library = "Delte dokumenter")
    Credential SharePointCert         — username = thumbprint, password = cert path
    Credential SharePointAPI          — username = tenant,     password = client id
    Credential KontAKTAPI             — username = base URL,    password = X-API-Key
"""
from OpenOrchestrator.orchestrator_connection.connection import OrchestratorConnection
from OpenOrchestrator.database.queues import QueueElement
import json
import tempfile
from pathlib import Path

import requests

from robot_framework import reset
from oomtm import go as oomtm_go
from oomtm import pdf as oomtm_pdf
from oomtm import sharepoint as sp

LIBRARY = "Delte dokumenter"


def process(
    orchestrator_connection: OrchestratorConnection,
    queue_element: QueueElement | None = None,
    client: "reset.Client | None" = None,
) -> None:
    orchestrator_connection.log_trace("Running process.")
    if queue_element is None:
        raise RuntimeError("KontAKTGOToPDF is queue-driven; no queue_element given.")
    if client is None:  # e.g. a manual run outside the queue framework
        client = reset.open_all(orchestrator_connection)

    payload = json.loads(queue_element.data or "{}")
    case_id = int(payload["kontakt_case_id"])
    doc_id = int(payload["doc_id"])
    source_case_id = str(payload.get("source_case_id") or "").strip()
    dok_id = str(payload["dok_id"]).strip()
    akt_id = payload.get("akt_id")
    title = str(payload.get("title") or "").strip()
    case_title = str(payload.get("case_title") or "").strip()

    orchestrator_connection.log_info(f"GOToPDF case={case_id} doc={doc_id} dok={dok_id}")
    _callback(orchestrator_connection, client, case_id, doc_id, {"status": "converting"})

    try:
        result = _convert_and_upload(
            orchestrator_connection, client, case_id, source_case_id, dok_id, akt_id, title, case_title,
        )
    except Exception as exc:
        orchestrator_connection.log_info(f"GOToPDF failed: {exc!r}")
        _callback(orchestrator_connection, client, case_id, doc_id, {"status": "error", "note": str(exc)[:500]})
        raise

    _callback(orchestrator_connection, client, case_id, doc_id, result)
    orchestrator_connection.log_info(f"GOToPDF done doc={doc_id}: {result.get('status')}")


# ----- Conversion + upload ---------------------------------------------------


def _convert_and_upload(orchestrator_connection, client, case_id, source_case_id, dok_id, akt_id, title, case_title):
    with tempfile.TemporaryDirectory() as tmpdir:
        work = Path(tmpdir)
        upload_path, upload_ext, status, note = _prepare_file(
            orchestrator_connection, client, dok_id, work,
        )
        if status == "error" or upload_path is None:
            return {"status": "error", "note": note}

        # status is "ready" (PDF) or "uploaded_original" (couldn't convert — the
        # original is uploaded as-is so it's still in SharePoint). Both upload.
        result = _upload_final(
            orchestrator_connection, client, case_id, source_case_id, akt_id, dok_id, title,
            case_title, upload_path, upload_ext,
        )
        result["status"] = status
        if note:
            result["note"] = note
        return result


def _prepare_file(orchestrator_connection, client, dok_id, work):
    """Return (upload_path, upload_ext, status, note) for one GO document.

    status:
      * "ready"             — upload_path is a PDF
      * "uploaded_original" — couldn't convert; upload the original as-is
                              (still lands in SharePoint, just not OCR-screenable)
      * "error"            — couldn't fetch the file at all
    """
    session = client.go_session
    go_url = client.go_url
    meta = oomtm_go.fetch_metadata(session, base_url=go_url, dok_id=dok_id)
    ext = (meta.get("ext") or "").lower()
    version_ui = meta.get("version_ui")

    # Resolve .goref pointer documents to the real document id.
    if ext == "goref":
        ref_path = work / f"{dok_id}.goref"
        oomtm_go.download_file(session, base_url=go_url, dok_id=dok_id, local_path=str(ref_path))
        text = ref_path.read_text(encoding="utf-8", errors="ignore")
        if "?docid=" in text:
            dok_id = text.split("?docid=")[1].split('"')[0]
            orchestrator_connection.log_info(f"Resolved goref -> {dok_id}")
            meta = oomtm_go.fetch_metadata(session, base_url=go_url, dok_id=dok_id)
            ext = (meta.get("ext") or "").lower()
            version_ui = meta.get("version_ui")

    # Already a PDF — just download it.
    if ext == "pdf":
        out = work / f"{dok_id}.pdf"
        oomtm_go.download_file(session, base_url=go_url, dok_id=dok_id, local_path=str(out))
        return out, "pdf", "ready", ""

    kind = oomtm_pdf.classify(ext)

    # Prefer GO's built-in converter (authoritative; handles emails + office),
    # except for kinds we know can't become PDF (video/audio).
    if kind != "skip" and version_ui:
        pdf_bytes = oomtm_go.pdf_convert(
            username=client.go_user, password=client.go_pass,
            base_url=go_url, dok_id=dok_id, version_ui=version_ui,
        )
        if pdf_bytes:
            out = work / f"{dok_id}.pdf"
            out.write_bytes(pdf_bytes)
            return out, "pdf", "ready", ""
        orchestrator_connection.log_info("GO PDF conversion declined — falling back to oomtm.pdf")

    # Download the original — we'll either convert it or upload it as-is.
    src = work / f"{dok_id}.{ext or 'bin'}"
    oomtm_go.download_file(session, base_url=go_url, dok_id=dok_id, local_path=str(src))

    if kind == "skip":
        return src, (ext or "bin"), "uploaded_original", (
            f"Filtypen .{ext} kan ikke konverteres til PDF — uploadet som original "
            "(bliver ikke OCR-screenet)."
        )

    pdf_path, cstatus, cnote = oomtm_pdf.convert_to_pdf(
        src, ext, work, auto_install=True, log=orchestrator_connection.log_info,
    )
    if cstatus == "ready" and pdf_path is not None:
        return pdf_path, "pdf", "ready", ""
    # Conversion failed/declined — upload the original so it's still in SharePoint.
    return src, (ext or "bin"), "uploaded_original", (
        cnote or "Kunne ikke konverteres til PDF — original uploadet (bliver ikke OCR-screenet)."
    )


def _upload_final(orchestrator_connection, client, case_id, source_case_id, akt_id, dok_id,
                  title, case_title, upload_path, upload_ext):
    """Upload ``upload_path`` (a PDF or an unconvertible original) into the case's
    SharePoint folder and return the callback payload (sans status)."""
    ctx, site_url = client.sp_ctx, client.sp_site_url

    overmappe = sp.sanitize_segment(f"{case_id} - {case_title}")[:120].strip() or str(case_id)
    undermappe = sp.sanitize_segment(source_case_id)[:80].strip() or "ukendt-sag"

    base_path = sp.site_root_path(site_url) + "/" + LIBRARY + "/"
    akt = akt_id if akt_id is not None else 0
    safe_title = sp.sanitize_title(title)
    safe_title = sp.truncate_title(
        safe_title, base_path=base_path, overmappe=overmappe, undermappe=undermappe,
        akt_id=akt, dok_id=dok_id,
    )
    filename = sp.build_filename(akt, dok_id, safe_title, upload_ext)

    final = upload_path.parent / filename
    if upload_path != final:
        upload_path.replace(final)

    sha = oomtm_pdf.sha256_file(final)
    size = final.stat().st_size
    file_path = sp.upload_to_case_folder(
        ctx, site_url=site_url, library=LIBRARY,
        overmappe=overmappe, undermappe=undermappe, local_file=str(final),
    )
    return {
        "sharepoint_url": sp.file_browser_url(site_url, file_path),
        "file_name": filename,
        "file_size_bytes": size,
        "sha256": sha.hex(),
    }


# ----- KontAKT callback ------------------------------------------------------


def _callback(orchestrator_connection, client, case_id: int, doc_id: int, body: dict) -> None:
    try:
        requests.post(
            f"{client.kontakt_base}/api/v1/cases/{case_id}/documents/{doc_id}/file",
            headers={"X-API-Key": client.kontakt_key, "Content-Type": "application/json"},
            json=body, timeout=30,
        )
    except Exception as exc:  # pylint: disable=broad-except
        orchestrator_connection.log_info(f"Callback to KontAKT failed: {exc!r}")
