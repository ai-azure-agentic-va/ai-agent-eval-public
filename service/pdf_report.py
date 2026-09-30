"""Paginated PDF reports using the same reconciled results and policy as CSV."""

from __future__ import annotations

import io
from collections.abc import Mapping, Sequence
from pathlib import Path
from threading import Lock
from typing import Any
from xml.sax.saxutils import escape

import reportlab
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    CondPageBreak,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from service.metrics import case_result, metric_result, score_value
from service.reports import report_snapshot

_FONT = "EvalReportVera"
_BOLD = "EvalReportVeraBold"
_FONT_LOCK = Lock()
_INK = colors.HexColor("#17324D")
_MUTED = colors.HexColor("#53677B")
_LINE = colors.HexColor("#DCE5ED")
_PALE = colors.HexColor("#F3F6F9")
_MARGIN = 42
# SimpleDocTemplate's frame also has six points of padding on each side.
_WIDTH = A4[0] - 2 * _MARGIN - 12


def _register_fonts() -> None:
    # ReportLab ships these redistributable TrueType fonts with the package.
    # Embedding them keeps accents and common punctuation readable on the server
    # and in PDF viewers without relying on the host's installed fonts.
    with _FONT_LOCK:
        if _FONT not in pdfmetrics.getRegisteredFontNames():
            font_dir = Path(reportlab.__file__).parent / "fonts"
            pdfmetrics.registerFont(TTFont(_FONT, str(font_dir / "Vera.ttf")))
            pdfmetrics.registerFont(TTFont(_BOLD, str(font_dir / "VeraBd.ttf")))
            pdfmetrics.registerFontFamily(_FONT, normal=_FONT, bold=_BOLD)


def _display(value: Any) -> str:
    """Retain Python's full numeric precision, including zero and False."""
    return "-" if value is None or value == "" else str(value)


def _paragraph(value: Any, style: ParagraphStyle) -> Paragraph:
    # Never let saved questions/answers become ReportLab markup or image paths.
    glyphs = pdfmetrics.getFont(style.fontName).face.charWidths
    # Vera covers accents and common punctuation, but not every script/emoji.
    # Reversible escapes retain unsupported characters instead of black boxes.
    text = "".join(char if ord(char) in glyphs or char in "\n\r\t" else
                   (f"\\u{ord(char):04x}" if ord(char) <= 0xFFFF else f"\\U{ord(char):08x}")
                   for char in _display(value))
    text = escape(text).replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    return Paragraph(text.replace("\n", "<br/>"), style)


def _styles() -> dict[str, ParagraphStyle]:
    base = {"fontName": _FONT, "fontSize": 9, "leading": 13,
            "textColor": _INK, "alignment": TA_LEFT, "splitLongWords": 1,
            "allowWidows": 1, "allowOrphans": 1}
    body = ParagraphStyle("ReportBody", **base)
    return {
        "body": body,
        "title": ParagraphStyle("ReportTitle", parent=body, fontName=_BOLD,
                                fontSize=23, leading=29, spaceAfter=12),
        "section": ParagraphStyle("ReportSection", parent=body, fontName=_BOLD,
                                  fontSize=12, leading=16, spaceBefore=14,
                                  spaceAfter=8, keepWithNext=True),
        "label": ParagraphStyle("ReportLabel", parent=body, fontName=_BOLD,
                                fontSize=8, leading=11, textColor=_MUTED,
                                spaceBefore=10, spaceAfter=3, keepWithNext=True),
        "field_label": ParagraphStyle("ReportFieldLabel", parent=body, fontName=_BOLD,
                                      fontSize=8, leading=11, textColor=_MUTED,
                                      spaceBefore=10, spaceAfter=3),
        "cell": ParagraphStyle("ReportCell", parent=body, fontSize=8, leading=11),
        "head": ParagraphStyle("ReportHead", parent=body, fontName=_BOLD,
                               fontSize=7.5, leading=10, textColor=colors.white),
        "note": ParagraphStyle("ReportNote", parent=body, fontSize=8, leading=11.5,
                               textColor=_MUTED, spaceAfter=7),
    }


def _table(data: Sequence[Sequence[Any]], widths: list[float], styles: dict,
           *, header: bool = True) -> Table:
    cells = [[_paragraph(value, styles["head" if header and i == 0 else "cell"])
              for value in row] for i, row in enumerate(data)]
    table = Table(cells, colWidths=widths, hAlign="LEFT", repeatRows=1 if header else 0,
                  splitByRow=1, splitInRow=1)
    commands = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -1), 0.35, _LINE),
        ("ROWBACKGROUNDS", (0, 1 if header else 0), (-1, -1), [colors.white, _PALE]),
    ]
    if header:
        commands.append(("BACKGROUND", (0, 0), (-1, 0), _INK))
    table.setStyle(TableStyle(commands))
    return table


def _field(story: list, label: str, value: Any, styles: dict) -> None:
    story.append(CondPageBreak(45))
    story.append(_paragraph(label, styles["field_label"]))
    # Keep potentially very long responses outside table cells: paragraphs can
    # split naturally over as many pages as their complete content requires.
    # Separate physical lines also avoid ReportLab's repeated <br/> split bug
    # when a long response crosses a page immediately before an empty line.
    lines = _display(value).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for line in lines:
        story.append(_paragraph(line, styles["body"]) if line else Spacer(1, styles["body"].leading))


def _page(canvas, document) -> None:
    canvas.saveState()
    canvas.setFont(_BOLD, 7)
    canvas.setFillColor(_MUTED)
    canvas.drawString(_MARGIN, A4[1] - 29, "AGENT EVALUATION / RUN REPORT")
    canvas.setStrokeColor(_LINE)
    canvas.line(_MARGIN, 33, A4[0] - _MARGIN, 33)
    canvas.setFont(_FONT, 7)
    canvas.drawString(_MARGIN, 21, "Saved results and scoring policy")
    canvas.drawRightString(A4[0] - _MARGIN, 21, f"Page {document.page}")
    canvas.restoreState()


def run_pdf(run_id: str, meta: Mapping[str, Any],
            rows: Sequence[Mapping[str, Any]]) -> bytes:
    """Export every reconciled case with full content and unrounded scores."""
    snapshot = report_snapshot(meta, rows)
    rows, policy = snapshot["rows"], snapshot["policy"]
    metrics, summary = snapshot["metrics"], snapshot["summary"]
    selected, subset = snapshot["selected"], snapshot["subset"]
    _register_fonts()
    styles = _styles()
    story: list = [_paragraph("Agent evaluation report", styles["title"])]

    overview = [
        ("Run ID", run_id, "Run status", meta.get("status")),
        ("Dataset", meta.get("dataset_name"), "Version", meta.get("dataset_version")),
        ("Application", meta.get("application_key", ""),
         "Execution mode", meta.get("execution_mode", "parallel")),
        ("Judge model", meta.get("judge_model", ""), "Started at", meta.get("started_at", "")),
        ("Finished at", meta.get("finished_at", ""), "Scoring version", policy["version"]),
    ]
    story.append(_table(overview, [80, _WIDTH / 2 - 80] * 2, styles, header=False))
    story.append(_paragraph("Run progress and outcomes", styles["section"]))
    story.append(_table([
        ["Cases", "Completed", "Missing", "Not run", "Pending"],
        [summary["total"], summary["completed"], summary["missing"], summary["not_run"],
         summary["verdicts"]["PENDING"]],
    ], [_WIDTH / 5] * 5, styles))
    story.append(Spacer(1, 8))
    story.append(_table([
        ["PASS", "Quality FAIL", "ERROR", "N/A", "Pass rate (%)"],
        [summary["verdicts"]["PASS"], summary["verdicts"]["FAIL"],
         summary["verdicts"]["ERROR"], summary["verdicts"]["N/A"], summary["pass_rate"]],
    ], [_WIDTH * 0.17] * 4 + [_WIDTH * 0.32], styles))
    story.append(_paragraph(
        f"Completed = scored or failed attempts. Failed execution rows: {summary['failed']}. "
        "Missing results have an unknown outcome; not-run cases were not attempted.", styles["note"]))

    story.append(_paragraph("Scoring policy", styles["section"]))
    policy_text = (f"Version {policy['version']} | Score scale: {policy['scale_min']} to {policy['scale_max']} "
                   f"| Comparison: {policy['comparison']} | Case rule: {policy['case_rule']}")
    if policy["version"] == 2:
        policy_text += "\nSelected metrics: " + (", ".join(selected) if subset else "all")
    story.append(_paragraph(policy_text, styles["note"]))
    story.append(_paragraph("Metric summary", styles["section"]))
    metric_rows = [["Metric", "Mean", "Count", "Threshold", "Pass rate (%)"]]
    for key in metrics:
        metric_rows.append([key, summary["means"].get(key), summary["metric_counts"][key],
                            policy["thresholds"][key], summary["metric_pass_rates"].get(key)])
    story.append(_table(metric_rows, [_WIDTH * factor for factor in (0.22, 0.23, 0.09, 0.23, 0.23)],
                        styles))
    scope = (
        "All selected metrics are shown; unselected metrics do not affect results."
        if subset else
        "Core quality metrics are shown; the overall case verdict includes all applicable category checks."
    )
    legend = (
        "PASS means score >= its saved threshold. Scores, means and pass rates are not rounded. "
        "Thresholds appear once per metric above. A dash means no recorded value; metric result labels "
        "distinguish N/A, PENDING, MISSING, NOT RUN and ERROR. Means use valid applicable scores only "
        "with a separate denominator for each metric; zero is a valid v2 score. "
        "Case pass rate = PASS / (PASS + FAIL + ERROR); v1 excludes errors from that denominator. "
        "Pending, missing, not-run and N/A cases are excluded. "
        "v2 requires all selected applicable metrics to pass; v1 retains accuracy-only results. "
        + scope + " Characters unavailable in the embedded font are shown as Unicode escapes."
    )
    story.append(Spacer(1, 10))
    story.append(_paragraph(legend, styles["note"]))

    if rows:
        story.append(PageBreak())
        story.append(_paragraph("Case details", styles["title"]))
    else:
        story.append(_paragraph("No case rows are available in this report.", styles["note"]))
    for position, row in enumerate(rows):
        story.append(CondPageBreak(92))
        story.append(_paragraph(
            f"Case {position + 1} - {_display(row.get('name'))}", styles["section"]))
        story.append(_table([
            ["Index", "Execution status", "Evaluation result"],
            [row.get("index"), row.get("status"), case_result(row, policy)],
        ], [_WIDTH * 0.15, _WIDTH * 0.40, _WIDTH * 0.45], styles))
        _field(story, "Question", row.get("question"), styles)
        _field(story, "Expected response", row.get("reference_answer", ""), styles)
        _field(story, "Actual response", row.get("response", ""), styles)
        story.append(_paragraph("Scores", styles["label"]))
        score_rows = [["Metric", "Score", "Result"]]
        values = row.get("metric_values") or {}
        for key in metrics:
            result = metric_result(row, key, policy)
            score = score_value(values.get(key), policy["version"]) if result in ("PASS", "FAIL") else None
            score_rows.append([key, score, result])
        story.append(_table(score_rows, [_WIDTH * 0.42, _WIDTH * 0.34, _WIDTH * 0.24], styles))
        story.append(_paragraph("Execution details", styles["label"]))
        story.append(_table([
            ("Latency (s)", row.get("latency_s")),
            ("Agent latency (s)", row.get("agent_latency_s")),
            ("Attempts", row.get("attempts", 1)),
            ("Usage tokens", row.get("usage_tokens")),
            ("Expected agent", row.get("expected_agent", "")),
            ("Routing observed", row.get("routing_observed")),
            ("Routing match", row.get("routing_match")),
        ], [108, _WIDTH - 108], styles, header=False))
        _field(story, "Error", row.get("error", ""), styles)
        story.append(Spacer(1, 14))
    story.append(_paragraph(
        "Routing match is True when every expected agent was invoked; a dash indicates no recorded "
        "value or no routing expectation. Full diagnostics remain in the results page.", styles["note"]))

    output = io.BytesIO()
    document = SimpleDocTemplate(output, pagesize=A4, leftMargin=_MARGIN, rightMargin=_MARGIN,
                                 topMargin=48, bottomMargin=45, title="Agent evaluation report",
                                 author="Agent Evaluation Framework", pageCompression=1)
    document.build(story, onFirstPage=_page, onLaterPages=_page)
    return output.getvalue()
