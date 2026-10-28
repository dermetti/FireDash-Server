"""On-demand A4 rendering. No HTML engine, network access, images or PDF spool."""

import io
from datetime import datetime
from pathlib import Path
from threading import Lock
from xml.sax.saxutils import escape

import reportlab
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle

from apps.report_transfer.contract import require
from apps.report_transfer.documents import document_digest

_font_lock = Lock()


def fonts():
    with _font_lock:
        if "ReportVera" not in pdfmetrics.getRegisteredFontNames():
            directory = Path(reportlab.__file__).parent / "fonts"
            pdfmetrics.registerFont(TTFont("ReportVera", str(directory / "Vera.ttf")))
            pdfmetrics.registerFont(TTFont("ReportVeraBold", str(directory / "VeraBd.ttf")))


def printable(value):
    # Preserve unsupported Unicode visibly rather than silently printing missing glyphs.
    # The bundled portable font covers Western European text. No host-font dependency.
    supported = pdfmetrics.getFont("ReportVera").face.charToGlyph
    return "".join(c if ord(c) in supported or c in "\n\t" else f"[U+{ord(c):04X}]" for c in value)


def display(value):
    if value["type"] == "boolean":
        return "Yes" if value["value"] else "No"
    return str(value["value"])


@sensitive_variables()
def render_pdf(document, *, attachment_hashes, received_at, exported_at=None):
    """Validate every call, return PDF bytes, and never change download/expiry state."""
    digest = document_digest(document, attachment_hashes)
    exported_at = exported_at or timezone.now()
    require(
        all(isinstance(t, datetime) and timezone.is_aware(t) for t in (received_at, exported_at))
    )
    fonts()
    body = ParagraphStyle(
        "ReportBody",
        fontName="ReportVera",
        fontSize=9,
        leading=13,
        spaceAfter=5,
        splitLongWords=True,
    )
    heading = ParagraphStyle(
        "ReportHeading",
        parent=body,
        fontName="ReportVeraBold",
        fontSize=12,
        leading=16,
        spaceBefore=10,
        spaceAfter=7,
    )
    title_style = ParagraphStyle("ReportTitle", parent=heading, fontSize=17, leading=22)

    def paragraph(value, style=body):
        return Paragraph(
            escape(printable(str(value))).replace("\n", "<br/>").replace("\t", "    "), style
        )

    story = [paragraph(document["title"], title_style)]
    author = document["finalization"]["author"]
    metadata = [
        ("Report ID", document["report_id"]),
        ("Report type", document["report_type"]),
        ("Finalizing person (client-declared)", f"{author['display_name']} ({author['id']})"),
        ("App finalization (client-declared)", document["finalization"]["finalized_at"]),
        ("Server receipt (server-observed)", received_at.isoformat()),
        ("PDF export (server-observed)", exported_at.isoformat()),
        ("Finalized document SHA-256", digest),
    ]
    story.extend(paragraph(f"{label}: {value}") for label, value in metadata)

    def render_fields(items):
        for item in items:
            story.append(paragraph(f"{item['label']}: {display(item['value'])}"))

    render_fields(document["header"])
    numbers = {a["id"]: i for i, a in enumerate(document["attachments"], 1)}
    for section in document["sections"]:
        story.append(paragraph(section["title"], heading))
        for block in section["blocks"]:
            kind = block["kind"]
            if kind == "text":
                story.append(paragraph(block["text"]))
            elif kind == "fields":
                render_fields(block["fields"])
            elif kind == "attachment_refs":
                story.append(
                    paragraph(
                        "Attachments: "
                        + ", ".join(str(numbers[i]) for i in block["attachment_ids"])
                    )
                )
            elif kind == "table":
                width = (A4[0] - 36 * mm) / len(block["columns"])
                headers = [paragraph(c["label"]) for c in block["columns"]]
                # A repeated header cannot itself split. Long labels (including
                # expanded Unicode fallbacks) get a flowing legend and short headers.
                if any(p.wrap(width - 10, A4[1])[1] > 100 for p in headers):
                    story.extend(
                        paragraph(f"Column {i}: {c['label']}")
                        for i, c in enumerate(block["columns"], 1)
                    )
                    headers = [paragraph(str(i)) for i in range(1, len(headers) + 1)]
                data = [headers]
                data.extend([paragraph(display(c)) for c in row] for row in block["rows"])
                # Split even a single tall cell; repeat column headings on each page.
                table = LongTable(
                    data,
                    colWidths=[width] * len(block["columns"]),
                    repeatRows=1,
                    splitByRow=1,
                    splitInRow=1,
                    hAlign="LEFT",
                )
                table.setStyle(
                    TableStyle(
                        [
                            ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
                            ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
                            ("LEFTPADDING", (0, 0), (-1, -1), 5),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                        ]
                    )
                )
                story.extend([table, Spacer(1, 6)])
    story.append(paragraph("JPEG attachments (download separately)", heading))
    if not numbers:
        story.append(paragraph("None"))
    for attachment in document["attachments"]:
        identity = attachment["id"]
        story.append(paragraph(f"{numbers[identity]}. {attachment['title']}"))
        story.append(paragraph(attachment["caption"]))
        story.append(paragraph(f"ID: {identity}; SHA-256: {attachment_hashes[identity]}"))
    signature = document["finalization"].get("signature_attachment_id")
    if signature:
        story.append(
            paragraph(f"Signature reference (client-declared): Attachment {numbers[signature]}")
        )

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("ReportVera", 8)
        canvas.drawString(18 * mm, 12 * mm, f"Report {document['report_id']}")
        canvas.drawRightString(A4[0] - 18 * mm, 12 * mm, f"Page {doc.page}")
        canvas.restoreState()

    output = io.BytesIO()
    pdf = SimpleDocTemplate(
        output,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=20 * mm,
        title="FireDash Report",
        author="FireDash",
        pageCompression=1,
    )
    pdf.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()
