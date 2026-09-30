"""Small helpers shared by the ATS adapters (location text, company display names)."""

from typing import Any

_COUNTRIES = {
    "IN": "India", "IND": "India", "US": "United States", "USA": "United States",
    "GB": "United Kingdom", "UK": "United Kingdom", "CA": "Canada", "DE": "Germany",
    "SG": "Singapore", "AU": "Australia", "IE": "Ireland", "NL": "Netherlands",
    "FR": "France", "AE": "United Arab Emirates", "JP": "Japan", "BR": "Brazil",
    "PL": "Poland", "ES": "Spain", "MX": "Mexico", "PH": "Philippines",
}  # fmt: skip


def country_name(code: str | None) -> str | None:
    if not code:
        return None
    code = code.strip()
    return _COUNTRIES.get(code.upper(), code if len(code) > 3 else code.upper())


def location_text(places: list[str | None], country: str | None, remote: bool) -> str | None:
    """Readable location built only from what the source states, e.g. 'Noida, India' or
    'Remote - India'. The country is appended only when the source gives one."""
    seen: list[str] = []
    for place in places:
        text = (place or "").strip()
        if text and text.lower() not in (s.lower() for s in seen):
            seen.append(text)
    name = country_name(country)
    body = " | ".join(seen)
    if name and name.lower() not in body.lower():
        body = f"{body}, {name}" if body else name
    if remote and "remote" not in body.lower():
        body = f"Remote - {body}" if body else "Remote"
    return body or None


def company_entries(raw: list[Any]) -> list[tuple[str, str]]:
    """Config entries -> (slug, display name). Accepts 'paytm' or {slug: paytm, name: Paytm}."""
    out = []
    for entry in raw or []:
        if isinstance(entry, str):
            out.append((entry, entry))
        elif isinstance(entry, dict) and entry.get("slug"):
            out.append((str(entry["slug"]), str(entry.get("name") or entry["slug"])))
    return out


def number(value: Any) -> float | None:
    """A JSON number (not a bool, not a string) as float; anything else -> None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
