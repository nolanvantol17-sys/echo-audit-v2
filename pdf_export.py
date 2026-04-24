"""
pdf_export.py — Single-interaction PDF rendering for Echo Audit V2.

Stateless helper that renders one graded/no-answer/failed interaction as a
client-shareable PDF. Used by the export endpoints to bundle PDFs into ZIPs
alongside the call audio.

Public API:
    render_interaction_pdf(conn, interaction_id) -> bytes

The caller owns the database connection; this module never opens or closes
one (mirrors grader.py's stateless pattern). Flask-free — no app or
request imports — so it's unit-testable in isolation.

Branching by status:
    - 43 (graded):     full report (header, write-ups, rubric table, transcript)
    - 44 (no-answer):  slim report (header + "Call unanswered" note)
    - other:           defensive "in-progress" report (header + whatever data exists)
"""

import base64
import io
import logging
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate, Frame, HRFlowable, Image, KeepTogether, PageBreak,
    PageTemplate, Paragraph, Spacer, Table, TableStyle,
)
from reportlab.pdfgen import canvas as rl_canvas

from db import q

logger = logging.getLogger(__name__)


# ── Status constants (mirrored from interactions_routes; kept local so this
# module stays Flask-free and import-cycle-free). ──
STATUS_GRADED    = 43
STATUS_NO_ANSWER = 44


# ── Brand palette ──
NAVY       = colors.HexColor("#0f1f3d")
SLATE      = colors.HexColor("#475569")
TEXT_BODY  = colors.HexColor("#1e293b")
TEXT_SOFT  = colors.HexColor("#334155")
TEXT_MUTED = colors.HexColor("#64748b")
BORDER     = colors.HexColor("#e2e8f0")
SOFT_BG    = colors.HexColor("#f8fafc")
GREEN_BG   = colors.HexColor("#dcfce7")
GREEN_TXT  = colors.HexColor("#166534")
AMBER_BG   = colors.HexColor("#fef3c7")
AMBER_TXT  = colors.HexColor("#92400e")
RED_BG     = colors.HexColor("#fee2e2")
RED_TXT    = colors.HexColor("#991b1b")
GRAY_BG    = colors.HexColor("#e5e7eb")


# ── Logo cache (loaded once per process) ──
_LOGO_BYTES = None
_LOGO_PATH  = Path(__file__).parent / "static" / "logo_base64.txt"


def _logo_bytes():
    global _LOGO_BYTES
    if _LOGO_BYTES is None:
        try:
            _LOGO_BYTES = base64.b64decode(_LOGO_PATH.read_text().strip())
        except Exception:
            logger.warning("Failed to load logo from %s; PDFs will render without it", _LOGO_PATH)
            _LOGO_BYTES = b""
    return _LOGO_BYTES


# ── Score / formatting helpers ──

def _score_palette(score):
    """Return (bg_color, text_color) for a 0-10 score band."""
    if score is None:
        return GRAY_BG, SLATE
    s = float(score)
    if s >= 8: return GREEN_BG, GREEN_TXT
    if s >= 5: return AMBER_BG, AMBER_TXT
    return RED_BG, RED_TXT


def _fmt_date_long(d):
    return d.strftime("%B %d, %Y") if d else "—"


def _fmt_duration(seconds):
    if not seconds:
        return "—"
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def _fmt_score(score):
    if score is None:
        return "N/A"
    return f"{float(score):.1f}"


def _normalize_yes_no(score_value, score_type):
    """Map the stored numeric value to YES/NO/PENDING display."""
    sv = float(score_value)
    if score_type == "yes_no_pending" and abs(sv - 5) < 0.01:
        return "PENDING"
    return "YES" if sv >= 5 else "NO"


# ── Style sheet ──

def _make_styles():
    base = getSampleStyleSheet()
    out = {}
    out["meta_label"] = ParagraphStyle(
        "meta_label", parent=base["Normal"],
        fontName="Helvetica", fontSize=8, textColor=SLATE,
        leading=10, spaceAfter=1,
    )
    out["meta_value"] = ParagraphStyle(
        "meta_value", parent=base["Normal"],
        fontName="Helvetica-Bold", fontSize=10, textColor=NAVY,
        leading=12, spaceAfter=4,
    )
    out["section_header"] = ParagraphStyle(
        "section_header", parent=base["Heading2"],
        fontName="Helvetica-Bold", fontSize=12, textColor=NAVY,
        leading=14, spaceBefore=14, spaceAfter=6,
    )
    out["body"] = ParagraphStyle(
        "body", parent=base["Normal"],
        fontName="Helvetica", fontSize=9.5, textColor=TEXT_BODY,
        leading=14, spaceAfter=6, alignment=TA_LEFT,
    )
    # Hanging-bullet style: leftIndent positions the wrap, firstLineIndent
    # pulls the bullet back to the gutter so wraps align under the text.
    out["bullet"] = ParagraphStyle(
        "bullet", parent=out["body"],
        leftIndent=14, firstLineIndent=-14,
        bulletIndent=0, spaceAfter=4,
    )
    out["rubric_name"] = ParagraphStyle(
        "rubric_name", parent=base["Normal"],
        fontName="Helvetica-Bold", fontSize=10, textColor=NAVY,
        leading=12, spaceAfter=2,
    )
    out["rubric_explain"] = ParagraphStyle(
        "rubric_explain", parent=base["Normal"],
        fontName="Helvetica", fontSize=9, textColor=TEXT_SOFT,
        leading=12, spaceAfter=0,
    )
    out["transcript_line"] = ParagraphStyle(
        "transcript_line", parent=base["Normal"],
        fontName="Helvetica", fontSize=8.5, textColor=TEXT_BODY,
        leading=12, spaceAfter=2,
    )
    out["empty_note"] = ParagraphStyle(
        "empty_note", parent=base["Normal"],
        fontName="Helvetica-Oblique", fontSize=10, textColor=SLATE,
        leading=14, spaceBefore=20, alignment=TA_CENTER,
    )
    return out


# ── Numbered canvas for "Page X of Y" footer ──
# ReportLab needs two passes to know the total page count: the first pass
# defers footer drawing, then we backfill once we know N.
class _NumberedCanvas(rl_canvas.Canvas):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self._draw_footer(total)
            super().showPage()
        super().save()

    def _draw_footer(self, total):
        self.setFont("Helvetica", 8)
        self.setFillColor(TEXT_MUTED)
        page_w = letter[0]
        self.drawCentredString(page_w / 2, 0.35 * inch,
                               f"Page {self._pageNumber} of {total}")
        # Discrete brand mark on the footer left
        self.drawString(0.6 * inch, 0.35 * inch, "Echo Audit")


# ── Database fetches ──

def _fetch_interaction(conn, interaction_id):
    cur = conn.execute(
        q("""SELECT
                i.interaction_id, i.interaction_date, i.status_id,
                i.interaction_overall_score,
                i.interaction_strengths, i.interaction_weaknesses,
                i.interaction_overall_assessment,
                i.interaction_transcript, i.interaction_responder_name,
                i.interaction_call_duration_seconds,
                i.interaction_audio_url,
                p.project_name,
                c.campaign_name,
                loc.location_name,
                (caller.user_first_name || ' ' || caller.user_last_name) AS caller_name,
                (resp.user_first_name   || ' ' || resp.user_last_name)   AS respondent_name
             FROM interactions i
             JOIN projects p ON p.project_id = i.project_id
             LEFT JOIN campaigns c          ON c.campaign_id   = i.campaign_id
             LEFT JOIN locations loc        ON loc.location_id = i.interaction_location_id
             LEFT JOIN users caller         ON caller.user_id  = i.caller_user_id
             LEFT JOIN users resp           ON resp.user_id    = i.respondent_user_id
             WHERE i.interaction_id = ? AND i.interaction_deleted_at IS NULL"""),
        (interaction_id,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _fetch_rubric_scores(conn, interaction_id):
    cur = conn.execute(
        q("""SELECT irs_snapshot_name, irs_snapshot_score_type,
                    irs_snapshot_weight, irs_score_value,
                    irs_score_ai_explanation
             FROM interaction_rubric_scores
             WHERE interaction_id = ?
             ORDER BY interaction_rubric_score_id ASC"""),
        (interaction_id,),
    )
    return [dict(r) for r in cur.fetchall()]


# ── Builders for each section ──

def _build_header(intr, styles, *, show_score=True):
    """Title row (location + project/campaign + logo + optional score) and
    the metadata strip below it. Returns a list of flowables."""
    loc = intr.get("location_name") or "—"
    proj = intr.get("project_name") or ""
    camp = intr.get("campaign_name") or "No campaign"
    title_html = (
        f'<font name="Helvetica-Bold" size="16" color="#0f1f3d">{loc}</font>'
        f'<br/><font name="Helvetica" size="9" color="#64748b">'
        f'{proj} &middot; {camp}</font>'
    )
    title_p = Paragraph(title_html, ParagraphStyle("hdr_title", fontSize=16, leading=20))

    # Right column: logo always; score badge only when relevant.
    right_stack = []
    logo = _logo_bytes()
    if logo:
        right_stack.append([Image(io.BytesIO(logo), width=80, height=80*218/400)])
    if show_score:
        if right_stack:
            right_stack.append([Spacer(1, 4)])
        bg, _txt = _score_palette(intr.get("interaction_overall_score"))
        # NAVY digit on tinted background — better contrast than tinted-on-tinted.
        score_table = Table([
            [Paragraph(_fmt_score(intr.get("interaction_overall_score")),
                       ParagraphStyle("scv", fontName="Helvetica-Bold", fontSize=22,
                                      textColor=NAVY, alignment=TA_CENTER, leading=24))],
            [Paragraph("OVERALL",
                       ParagraphStyle("scl", fontName="Helvetica", fontSize=7,
                                      textColor=SLATE, alignment=TA_CENTER, leading=8))],
        ], colWidths=[80])
        score_table.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,-1), bg),
            ("TOPPADDING", (0,0), (-1,-1), 6),
            ("BOTTOMPADDING", (0,0), (-1,-1), 6),
            ("LEFTPADDING", (0,0), (-1,-1), 8),
            ("RIGHTPADDING", (0,0), (-1,-1), 8),
        ]))
        right_stack.append([score_table])

    if right_stack:
        right_col = Table(right_stack, colWidths=[80])
        right_col.setStyle(TableStyle([
            ("ALIGN", (0,0), (-1,-1), "RIGHT"),
            ("LEFTPADDING", (0,0), (-1,-1), 0),
            ("RIGHTPADDING", (0,0), (-1,-1), 0),
            ("TOPPADDING", (0,0), (-1,-1), 0),
            ("BOTTOMPADDING", (0,0), (-1,-1), 0),
        ]))
        header_row = Table([[title_p, right_col]], colWidths=[6.5*inch - 100, 90])
    else:
        header_row = Table([[title_p]], colWidths=[6.5*inch])
    header_row.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING", (0,0), (-1,-1), 0),
        ("RIGHTPADDING", (0,0), (-1,-1), 0),
        ("TOPPADDING", (0,0), (-1,-1), 0),
        ("BOTTOMPADDING", (0,0), (-1,-1), 0),
    ]))

    # Metadata strip
    cells = [
        ("Date",       _fmt_date_long(intr.get("interaction_date"))),
        ("Caller",     intr.get("caller_name") or "—"),
        ("Respondent", intr.get("interaction_responder_name")
                       or intr.get("respondent_name") or "—"),
        ("Duration",   _fmt_duration(intr.get("interaction_call_duration_seconds"))),
    ]
    label_row = [Paragraph(label.upper(), styles["meta_label"]) for label, _ in cells]
    value_row = [Paragraph(str(value),  styles["meta_value"]) for _,    value in cells]
    meta_grid = Table([label_row, value_row], colWidths=[1.6*inch] * 4)
    meta_grid.setStyle(TableStyle([
        ("LINEBELOW", (0,1), (-1,1), 0.5, BORDER),
        ("LINEABOVE", (0,0), (-1,0), 0.5, BORDER),
        ("BACKGROUND", (0,0), (-1,-1), SOFT_BG),
        ("TOPPADDING", (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
        ("LEFTPADDING", (0,0), (-1,-1), 10),
        ("RIGHTPADDING", (0,0), (-1,-1), 10),
    ]))

    return [header_row, Spacer(1, 14), meta_grid]


def _build_writeups(intr, styles):
    """Strengths / Weaknesses / Overall Assessment sections, with proper
    hanging-bullet indent for any bulleted lines."""
    out = []

    def section(title, text):
        if not text:
            return
        out.append(Paragraph(title, styles["section_header"]))
        # Split into paragraphs; render bulleted lines as hanging bullets and
        # plain prose as a body paragraph.
        for raw in text.split("\n"):
            line = raw.strip()
            if not line:
                continue
            if line.startswith("•") or line.startswith("-") or line.startswith("*"):
                # Strip leading marker + whitespace, then render as proper bullet
                content = line.lstrip("•-* ").strip()
                out.append(Paragraph(f"&bull; {content}", styles["bullet"]))
            else:
                out.append(Paragraph(line, styles["body"]))

    section("Strengths",               intr.get("interaction_strengths"))
    section("Areas for Improvement",   intr.get("interaction_weaknesses"))
    section("Overall Assessment",      intr.get("interaction_overall_assessment"))
    return out


def _build_rubric(scores, styles):
    if not scores:
        return []
    out = [Paragraph("Rubric Breakdown", styles["section_header"])]
    table_data = []
    style = TableStyle([
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("TOPPADDING", (0,0), (-1,-1), 8),
        ("BOTTOMPADDING", (0,0), (-1,-1), 8),
        ("LEFTPADDING", (0,0), (-1,-1), 10),
        ("RIGHTPADDING", (0,0), (-1,-1), 10),
        ("LINEBELOW", (0,0), (-1,-2), 0.5, BORDER),
        ("BOX", (0,0), (-1,-1), 0.5, BORDER),
    ])

    for i, s in enumerate(scores):
        sv = float(s["irs_score_value"])
        st = s["irs_snapshot_score_type"]
        if st in ("yes_no", "yes_no_pending"):
            display = _normalize_yes_no(sv, st)
            # For tint, treat YES as high-band, NO as low-band, PENDING as mid.
            tint_score = 10 if display == "YES" else (5 if display == "PENDING" else 0)
        else:
            display = f"{sv:.0f}/10"
            tint_score = sv
        bg, fg = _score_palette(tint_score)

        score_cell = Paragraph(
            f'<font color="{fg.hexval()}"><b>{display}</b></font>',
            ParagraphStyle("rs", fontSize=9.5, alignment=TA_CENTER, leading=12),
        )
        name_cell    = Paragraph(s["irs_snapshot_name"], styles["rubric_name"])
        explain_html = (s["irs_score_ai_explanation"] or "").replace("\n", "<br/>")
        explain_cell = Paragraph(explain_html, styles["rubric_explain"])

        table_data.append([
            [name_cell, Spacer(1, 2), explain_cell],
            score_cell,
        ])
        style.add("BACKGROUND", (1, i), (1, i), bg)

    rubric_table = Table(table_data, colWidths=[5.3*inch, 0.9*inch])
    rubric_table.setStyle(style)
    out.append(rubric_table)
    return out


def _build_transcript(intr, styles):
    """Transcript with thin rule between speaker turns to aid scanning."""
    text = intr.get("interaction_transcript") or ""
    if not text:
        return []
    out = [Paragraph("Full Transcript", styles["section_header"])]
    last_speaker = None
    rule = HRFlowable(width="100%", thickness=0.25, color=BORDER,
                      spaceBefore=4, spaceAfter=4)
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        # Detect "Speaker X:" — both bare ("Speaker A:") and timestamped
        # ("[0:06] Speaker A: …"). Used to insert a separator on speaker change.
        speaker = None
        for marker in ("Speaker A:", "Speaker B:", "Speaker C:", "Speaker D:"):
            if marker in line:
                speaker = marker.rstrip(":")
                line = line.replace(marker, f"<b>{marker}</b>")
                break
        if last_speaker is not None and speaker is not None and speaker != last_speaker:
            out.append(rule)
        if speaker is not None:
            last_speaker = speaker
        out.append(Paragraph(line, styles["transcript_line"]))
    return out


# ── Document assembly ──

def _make_doc(buf, intr):
    title = intr.get("location_name") or f"Interaction {intr.get('interaction_id')}"
    doc = BaseDocTemplate(
        buf, pagesize=letter,
        leftMargin=0.6*inch, rightMargin=0.6*inch,
        topMargin=0.5*inch,  bottomMargin=0.7*inch,
        title=f"Call Report — {title}", author="Echo Audit",
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin,
                  doc.width, doc.height, id="main",
                  leftPadding=0, rightPadding=0,
                  topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="default", frames=[frame])])
    return doc


def _build_story(intr, scores, styles):
    """Compose the flowable story for this interaction's variant."""
    status = intr.get("status_id")
    story = []

    if status == STATUS_NO_ANSWER:
        # Slim variant: header (no score badge), then a single note.
        story.extend(_build_header(intr, styles, show_score=False))
        story.append(Paragraph(
            "This call was logged as <b>unanswered</b>. No grade or transcript is available.",
            styles["empty_note"],
        ))
        if intr.get("interaction_audio_url"):
            story.append(Paragraph(
                "An audio recording is included with this report.",
                styles["empty_note"],
            ))
        return story

    if status != STATUS_GRADED:
        # Failed / in-progress fallback: header with score (which may be None),
        # then any partial fields that exist. Never crashes on missing data.
        story.extend(_build_header(intr, styles, show_score=True))
        story.append(Paragraph(
            "This call did not finish grading. Showing whatever data was captured.",
            styles["empty_note"],
        ))
        story.extend(_build_writeups(intr, styles))
        story.extend(_build_rubric(scores, styles))
        if intr.get("interaction_transcript"):
            story.append(PageBreak())
            story.extend(_build_transcript(intr, styles))
        return story

    # Graded variant: full report.
    story.extend(_build_header(intr, styles, show_score=True))
    story.extend(_build_writeups(intr, styles))
    story.extend(_build_rubric(scores, styles))
    if intr.get("interaction_transcript"):
        story.append(PageBreak())
        story.extend(_build_transcript(intr, styles))
    return story


# ── Public entry point ──

def render_interaction_pdf(conn, interaction_id):
    """Render a single interaction as a PDF report. Returns bytes.

    Raises ValueError if the interaction does not exist (or is soft-deleted).
    Caller is responsible for tenant-scoping the lookup before calling — this
    function will render whatever interaction_id it is handed.
    """
    intr = _fetch_interaction(conn, interaction_id)
    if intr is None:
        raise ValueError(f"Interaction {interaction_id} not found")
    scores = _fetch_rubric_scores(conn, interaction_id) \
             if intr.get("status_id") == STATUS_GRADED else []

    styles = _make_styles()
    buf    = io.BytesIO()
    doc    = _make_doc(buf, intr)
    story  = _build_story(intr, scores, styles)
    doc.build(story, canvasmaker=_NumberedCanvas)
    return buf.getvalue()
