"""Render a validated tailored resume to Markdown and PDF artifacts (new files only)."""

from pathlib import Path
from typing import Any

from backend.core.profile import UserProfile

# Core PDF fonts are Latin-1 only: typographic characters are mapped, never facts changed.
_PDF_CHARS = {
    "–": "-",
    "—": "-",
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "•": "-",
    "∼": "~",
    "…": "...",
    "₹": "Rs.",
    "≥": ">=",
    "≤": "<=",
    "→": "->",
    " ": " ",
}

SECTION_TITLES = {
    "summary": "Summary",
    "skills": "Technical Skills",
    "experience": "Experience",
    "projects": "Projects",
    "education": "Education",
    "publications": "Publications",
}


def _header(profile: UserProfile) -> tuple[str, str]:
    f = profile.verified_profile
    contact = [x for x in [f.phone, str(f.email), f.location, *f.links.values()] if x]
    return f.name, " | ".join(contact)


def _dates(start: str, end: str) -> str:
    return f"{start} - {end or 'Present'}"


def _blocks(content: dict[str, Any]) -> list[tuple[str, list[str]]]:
    """(section title, lines) in the tailored order. Lines starting with '- ' are bullets."""
    out: list[tuple[str, list[str]]] = []
    for section in content["section_order"]:
        lines: list[str] = []
        if section == "summary" and content["summary"]:
            lines = [content["summary"]]
        elif section == "skills":
            lines = [
                f"{g['category']}: {', '.join(g['items'])}" for g in content["skills"] if g["items"]
            ]
        elif section == "experience":
            for e in content["experience"]:
                lines.append(
                    f"**{e['title']}**, {e['company']} ({e['location']}) | "
                    f"{_dates(e['start_date'], e['end_date'])}"
                )
                lines += [f"- {b}" for b in e["bullets"]]
        elif section == "projects":
            for p in content["projects"]:
                lines.append(f"**{p['name']}** | {', '.join(p['technologies'])}")
                lines += [f"- {b}" for b in p["bullets"]]
        elif section == "education":
            for e in content["education"]:
                grade = f" | {e['grade']}" if e["grade"] else ""
                lines.append(
                    f"**{e['institution']}**, {e['degree']} in {e['field_of_study']}"
                    f" ({e['graduation_year']}){grade}"
                )
        elif section == "publications":
            lines = [f"- {p}" for p in content["publications"]]
        if lines:
            out.append((SECTION_TITLES[section], lines))
    if content.get("certifications"):
        out.append(("Certifications", [f"- {c}" for c in content["certifications"]]))
    return out


def render_markdown(content: dict[str, Any], profile: UserProfile) -> str:
    name, contact = _header(profile)
    parts = [f"# {name}", contact, ""]
    for title, lines in _blocks(content):
        parts += [f"## {title}", *lines, ""]
    return "\n".join(parts).rstrip() + "\n"


def _pdf_text(text: str) -> str:
    for src, dst in _PDF_CHARS.items():
        text = text.replace(src, dst)
    return text.encode("latin-1", "replace").decode("latin-1")


def render_pdf(content: dict[str, Any], profile: UserProfile) -> bytes:
    from fpdf import FPDF

    name, contact = _header(profile)
    pdf = FPDF(format="A4")
    pdf.set_margins(15, 12, 15)
    pdf.set_auto_page_break(True, margin=12)
    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(width, 9, _pdf_text(name), align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.multi_cell(width, 4.5, _pdf_text(contact), align="C", new_x="LMARGIN", new_y="NEXT")
    for title, lines in _blocks(content):
        pdf.ln(2)
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(width, 6, _pdf_text(title.upper()), new_x="LMARGIN", new_y="NEXT")
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + width, pdf.get_y())
        pdf.ln(1)
        for line in lines:
            bullet = line.startswith("- ")
            text = _pdf_text(("- " + line[2:]) if bullet else line)
            bold = text.startswith("**")
            pdf.set_font("Helvetica", "B" if bold else "", 9.5)
            pdf.multi_cell(width, 4.8, text.replace("**", ""), new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def write_new_file(path: Path, data: bytes) -> None:
    """Create a file that must not exist yet: versions are never overwritten."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as fh:  # 'x' fails if the file already exists
        fh.write(data)
