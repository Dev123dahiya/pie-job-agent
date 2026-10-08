"""Deterministic text normalization shared by job sources, deduplication and models.

Everything here is extraction or canonicalization of text that is actually present.
Nothing guesses: when a value cannot be found, callers get None / "unknown".
"""

import hashlib
import html
import re
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from database.enums import RemoteType

_LEGAL_SUFFIXES = {
    "inc", "incorporated", "llc", "llp", "ltd", "limited", "pvt", "private", "corp",
    "corporation", "co", "company", "gmbh", "plc", "sa", "ag", "bv", "pte",
}  # fmt: skip
_TITLE_REPLACEMENTS = {
    "sr": "senior",
    "snr": "senior",
    "jr": "junior",
    "engg": "engineer",
    "eng": "engineer",
    "mgr": "manager",
    "dev": "developer",
    "swe": "software engineer",
}
# Query parameters that only track where a click came from; never identify a job.
_TRACKING_PARAMS = {
    "gh_src", "source", "src", "ref", "refid", "referrer", "trk", "trkid", "lever-source",
    "lever-origin", "fbclid", "gclid", "mc_cid", "mc_eid", "_hsenc", "_hsmi", "sid",
}  # fmt: skip

_NON_ALNUM = re.compile(r"[^a-z0-9+#.]+")
_WS = re.compile(r"\s+")


def _tokens(value: str) -> list[str]:
    return [t.strip(".") for t in _NON_ALNUM.sub(" ", value.lower()).split() if t.strip(".")]


def normalize_company(name: str) -> str:
    """'Acme Technologies Pvt. Ltd.' -> 'acme technologies'; 'Acme Labs.ai' -> 'acme labs ai'."""
    tokens = _tokens(name.replace(".", " "))
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens) or name.strip().lower()


def normalize_title(title: str) -> str:
    """'Sr. Software Engg (Backend)' -> 'senior software engineer backend'."""
    out: list[str] = []
    for token in _tokens(title):
        out.extend(_TITLE_REPLACEMENTS.get(token, token).split())
    return " ".join(out)


def normalize_location(location: str | None) -> str:
    if not location:
        return ""
    text = " ".join(_tokens(location))
    return text.replace("gurgaon", "gurugram")


def canonicalize_url(url: str | None) -> str | None:
    """Stable URL for dedupe: lower-case host, no fragment/tracking params, sorted query."""
    if not url:
        return None
    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.netloc:
        return None
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if k.lower() not in _TRACKING_PARAMS and not k.lower().startswith("utm_")
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        ("https", parts.netloc.lower().removeprefix("www."), path, urlencode(query), "")
    )


def job_fingerprint(company: str | None, title: str, location: str | None) -> str:
    """Deterministic content fingerprint for the secondary duplicate check."""
    basis = "|".join(
        [normalize_company(company or ""), normalize_title(title), normalize_location(location)]
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


class _TextExtractor(HTMLParser):
    _BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        # (heading text, list items) pairs, used to find requirement sections.
        self.sections: list[tuple[str, list[str]]] = [("", [])]
        self._in_li = False
        self._li_buf: list[str] = []
        self._in_heading = False
        self._heading_buf: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._skip += 1
        if tag in self._BLOCK:
            self.parts.append("\n")
        if tag == "li":
            self._in_li, self._li_buf = True, []
            self.parts.append("- ")
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6", "strong", "b"}:
            self._in_heading, self._heading_buf = True, []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip:
            self._skip -= 1
        if tag == "li" and self._in_li:
            item = _WS.sub(" ", "".join(self._li_buf)).strip()
            if item:
                self.sections[-1][1].append(item)
            self._in_li = False
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6", "strong", "b"} and self._in_heading:
            heading = _WS.sub(" ", "".join(self._heading_buf)).strip()
            if heading and not self._in_li:
                self.sections.append((heading, []))
            self._in_heading = False
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        self.parts.append(data)
        if self._in_li:
            self._li_buf.append(data)
        if self._in_heading:
            self._heading_buf.append(data)


_REQUIREMENT_HEADING = re.compile(
    r"requirement|qualification|what you('|’)?ll (need|bring)|what we('|’)?re looking for|"
    r"must.have|you have|about you|who you are|skills|experience",
    re.I,
)


def html_to_text(content: str | None) -> tuple[str | None, list[str]]:
    """Convert (possibly entity-escaped) HTML to plain text + requirement bullet points."""
    if not content:
        return None, []
    raw = html.unescape(content) if "&lt;" in content else content
    parser = _TextExtractor()
    parser.feed(raw)
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")
    lines = [_WS.sub(" ", line).strip() for line in text.splitlines()]
    cleaned = "\n".join(line for line in lines if line)
    requirements = [
        item
        for heading, items in parser.sections
        if heading and _REQUIREMENT_HEADING.search(heading)
        for item in items
    ]
    return cleaned or None, requirements[:40]


_EXPERIENCE = re.compile(
    r"(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*(\d{1,2})\s*)?\+?\s*(?:years?|yrs?)"
    r"(?:\s+of)?(?:\s+\w+){0,4}?\s+(?:experience|exp\b)",
    re.I,
)
_FRESHER = re.compile(
    r"\b(fresher|freshers|new grad(uate)?s?|entry[- ]level|0\s*-\s*1\s*years?)\b", re.I
)


def extract_experience(text: str | None) -> str | None:
    """Return the experience requirement exactly as phrased ('3+ years', '2-4 years'), or None."""
    if not text:
        return None
    match = _EXPERIENCE.search(text)
    if match:
        low, high = match.group(1), match.group(2)
        if high:
            return f"{low}-{high} years"
        plus = "+" if re.search(r"\+|plus", match.group(0)) else ""
        unit = "year" if low == "1" and not plus else "years"
        return f"{low}{plus} {unit}"
    fresher = _FRESHER.search(text)
    return fresher.group(0).strip() if fresher else None


# Seniority evidence, used ONLY to order work (which employer's board to look for first).
# It never decides eligibility: that stays with the analysis and the shortlist rules.
_ENTRY_TITLE = re.compile(
    r"\b(graduate|new grad|fresher|trainee|junior|jr\.?|entry[\s-]level|"
    r"sde\s*-?\s*(?:i|1)|(?:software\s+)?engineer\s*-?\s*(?:i|1))\b(?!\s*\+)",
    re.I,
)
_ENTRY_TEXT = re.compile(
    r"\b(freshers?|new grad(?:uate)?s?|recent graduates?|entry[\s-]level|"
    r"0\s*(?:-|–|to)\s*[12]\s*\+?\s*(?:years?|yrs?)|no prior (?:work )?experience)\b",
    re.I,
)
_SENIOR_TITLE = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|manager|architect|head|director|"
    r"ii|iii|iv|[2-5])\b",
    re.I,
)
_INTERN = re.compile(r"\bintern(ship)?\b", re.I)


def required_years(text: str | None) -> float | None:
    """Smallest explicit 'N (+|-M) years ... experience' requirement in the text, or None.
    Needs the word 'experience' nearby (so '10 years ago' or '5 years of history' never
    count)."""
    values = [float(m.group(1)) for m in _EXPERIENCE.finditer(text or "")]
    return min(values) if values else None


def seniority_evidence(title: str, text: str | None, candidate_years: float | None) -> str:
    """'entry' / 'senior' / 'none' - what the posting itself says about the level it hires at.

    senior  an explicit experience requirement above the candidate's verified years, or a
            senior/numbered-level title (Senior, Staff, Lead, II, III...) - senior words win
            over entry words ("Senior Engineer I" is senior)
    entry   otherwise, an entry-level title (Graduate, Junior, Trainee, SDE I...) or wording
            (fresher, new grad, entry level, 0-1 years...)
    none    no evidence either way; internships are never 'entry' (not full-time)
    """
    years = required_years(text)
    if years is not None and candidate_years is not None and years > candidate_years:
        return "senior"
    if _SENIOR_TITLE.search(title):
        return "senior"
    if _INTERN.search(title):
        return "none"
    if _ENTRY_TITLE.search(title) or _ENTRY_TEXT.search(text or ""):
        return "entry"
    return "none"


def detect_remote_type(*texts: str | None) -> RemoteType:
    """Only returns REMOTE/HYBRID when the text says so; otherwise UNKNOWN (not 'onsite')."""
    joined = " ".join(t for t in texts if t).lower()
    if re.search(r"\bhybrid\b", joined):
        return RemoteType.HYBRID
    if re.search(r"\bremote\b|work from home|\bwfh\b", joined):
        return RemoteType.REMOTE
    if re.search(r"\bon-?site\b|in[- ]office", joined):
        return RemoteType.ONSITE
    return RemoteType.UNKNOWN


def extract_skills(text: str | None, vocabulary: list[str]) -> list[str]:
    """Skills from `vocabulary` that literally appear in the text (whole-word match)."""
    if not text:
        return []
    found = []
    for skill in vocabulary:
        pattern = r"(?<![A-Za-z0-9])" + re.escape(skill) + r"(?![A-Za-z0-9])"
        if re.search(pattern, text, re.I):
            found.append(skill)
    return found


def title_matches(title: str, terms: list[str], aliases: dict[str, list[str]]) -> bool:
    """True if the title contains any search term (or one of its aliases) as whole words."""
    norm_title = f" {normalize_title(title)} "
    for term in terms:
        for variant in [term, *aliases.get(term, [])]:
            if f" {normalize_title(variant)} " in norm_title:
                return True
    return False


# Salary periods PIE can annualise without assuming anything (12 months in a year). Hourly,
# daily and weekly pay would need an assumed number of working hours/days, so they are kept
# only as the raw source value.
_ANNUAL_FACTOR = {"YEAR": 1, "MONTH": 12}
_PERIOD_ALIASES = {
    "year": "YEAR", "yearly": "YEAR", "annual": "YEAR", "annually": "YEAR",
    "per-year-salary": "YEAR", "1 year": "YEAR", "per year": "YEAR",
    "month": "MONTH", "monthly": "MONTH", "per-month-salary": "MONTH", "1 month": "MONTH",
    "hour": "HOUR", "hourly": "HOUR", "per-hour-wage": "HOUR", "1 hour": "HOUR",
    "week": "WEEK", "weekly": "WEEK", "per-week-salary": "WEEK", "1 week": "WEEK",
    "day": "DAY", "daily": "DAY", "per-day-wage": "DAY", "1 day": "DAY",
    "one-time": "ONE_TIME",
}  # fmt: skip


def normalize_period(period: str | None) -> str | None:
    if not period:
        return None
    return _PERIOD_ALIASES.get(period.strip().lower(), "UNKNOWN")


def normalize_salary(
    low: float | None, high: float | None, currency: str | None, period: str | None
) -> tuple[float | None, float | None, str | None, str | None]:
    """(annual_min, annual_max, currency, period) from a DISCLOSED salary.

    Yearly figures are kept; monthly figures are multiplied by 12. Anything else (hourly,
    unknown period, non-positive or inverted ranges, no currency) gives no normalized value:
    the salary stays UNKNOWN for matching, and the caller keeps the raw source value."""
    norm_period = normalize_period(period)
    values = [v for v in (low, high) if v is not None]
    if not values or not currency or any(v <= 0 for v in values):
        return None, None, None, norm_period
    factor = _ANNUAL_FACTOR.get(norm_period or "")
    if factor is None:
        return None, None, None, norm_period
    lo = low * factor if low is not None else None
    hi = high * factor if high is not None else None
    if lo is not None and hi is not None and hi < lo:
        return None, None, None, norm_period
    return lo, hi, currency.upper(), norm_period


_MONEY = re.compile(
    r"^\s*(?P<cur>₹|rs\.?|inr|\$|usd|€|eur|£|gbp)\s*(?P<amount>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?:(?P<lpa>lpa)|(?P<unit>lakhs?|l|k)?\s*(?:/|per\s+)\s*"
    r"(?P<period>year|yr|annum|month|mo|hour|hr))\s*$",
    re.I,
)
_CURRENCY = {"₹": "INR", "rs": "INR", "rs.": "INR", "inr": "INR", "$": "USD", "usd": "USD",
             "€": "EUR", "eur": "EUR", "£": "GBP", "gbp": "GBP"}  # fmt: skip
_UNIT = {"lakh": 100_000, "lakhs": 100_000, "lpa": 100_000, "l": 100_000, "k": 1_000}
_TEXT_PERIOD = {"year": "YEAR", "yr": "YEAR", "annum": "YEAR", "month": "MONTH", "mo": "MONTH",
                "hour": "HOUR", "hr": "HOUR"}  # fmt: skip


def parse_salary_text(text: str | None) -> tuple[float | None, str | None, str | None]:
    """(amount, currency, period) from an EXPLICIT single-figure salary string such as
    '₹8,00,000/year' or '₹80,000 per month'. Anything else - a range written in words,
    'Competitive salary', 'Negotiable', a figure without a period - gives (None, None, None).
    The caller applies normalize_salary (yearly kept, monthly x12, hourly unknown)."""
    if not text:
        return None, None, None
    m = _MONEY.match(text.strip())
    if not m:
        return None, None, None
    amount = float(m.group("amount").replace(",", ""))
    currency = _CURRENCY.get(m.group("cur").lower())
    if m.group("lpa"):  # "LPA" = lakh per annum: the period is stated
        return amount * 100_000, currency, "YEAR"
    amount *= _UNIT.get((m.group("unit") or "").lower(), 1)
    return amount, currency, _TEXT_PERIOD[m.group("period").lower()]


# City names (and common alternate spellings) used ONLY to recognise the same posting across
# sources that write a location differently ("Gurugram" vs "Gurgaon, Haryana, India").
_CITY_ALIASES = {
    "gurugram": "gurugram", "gurgaon": "gurugram", "noida": "noida", "greater noida": "noida",
    "new delhi": "delhi", "delhi": "delhi", "faridabad": "faridabad", "ghaziabad": "ghaziabad",
    "bengaluru": "bengaluru", "bangalore": "bengaluru", "mumbai": "mumbai", "pune": "pune",
    "hyderabad": "hyderabad", "chennai": "chennai", "kolkata": "kolkata",
    "ahmedabad": "ahmedabad", "jaipur": "jaipur",
}  # fmt: skip


def city_key(location: str | None) -> str | None:
    """The one known city a location names, or None (unknown, or several different cities)."""
    text = f" {' '.join(_tokens(location or ''))} "
    found = {city for name, city in _CITY_ALIASES.items() if f" {name} " in text}
    return found.pop() if len(found) == 1 else None


_INR_LAKH = re.compile(
    r"(?i)(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)\s*(?:(?:-|–|to)\s*(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)\s*)?"
    r"(?:lacs?|lakhs?)\s*(?:p\.?\s*a\.?|per\s+annum|/\s*(?:yr|year|annum))"
    r"|(?i:(\d+(?:\.\d+)?)\s*(?:(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*)?lpa\b)"
)


def parse_inr_lakh_range(text: str | None) -> tuple[float, float] | None:
    """(min, max) ANNUAL rupees from Indian job-site wording that states the period:
    '4-8 Lacs P.A.', '12 Lakhs PA', '10-15 LPA'. Anything else (monthly, 'Not disclosed',
    no period) is None: a salary is never guessed."""
    m = _INR_LAKH.search(text or "")
    if not m:
        return None
    low_s, high_s = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
    low = float(low_s) * 100_000
    high = float(high_s) * 100_000 if high_s else low
    if high < low or high <= 0:
        return None
    return low, high
