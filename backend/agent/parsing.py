"""CUSTOM: deterministic extractors for how Pakistani property buyers write.

Used by the qualifier node (lead profile), the guard (PII) and the offline model.
Pure functions, unit-tested in tests/test_rules.py.
"""
import re
from datetime import datetime, timedelta

CRORE, LAKH = 10_000_000, 100_000

_NUM = r"(\d+(?:\.\d+)?)"
_UNIT = r"(crore|crores|cr|karor|lakh|lakhs|lac|lacs|million|mn|m|k)\b"
MONEY = re.compile(_NUM + r"\s*" + _UNIT, re.I)
MAX_WORDS = re.compile(r"\b(under|below|upto|up to|max(?:imum)?|within|less than|not more than|budget(?: is| of)?|around|about|till)\s*(?:of\s*)?(?:pkr|rs\.?)?\s*$", re.I)
MIN_WORDS = re.compile(r"\b(above|over|more than|at least|minimum|min)\s*(?:pkr|rs\.?)?\s*$", re.I)


def _unit_value(num: str, unit: str) -> int:
    n, u = float(num), unit.lower()
    if u in ("crore", "crores", "cr", "karor"):
        return int(n * CRORE)
    if u in ("lakh", "lakhs", "lac", "lacs"):
        return int(n * LAKH)
    if u in ("million", "mn", "m"):
        return int(n * 1_000_000)
    return int(n * 1_000)


def parse_budget(text: str) -> dict:
    """'under 2 crore' -> {'max': 20000000}; 'between 1 and 1.5 crore' -> {'min','max'}; '50 lakh' -> max."""
    out: dict = {}
    between = re.search(_NUM + r"\s*(" + _UNIT[1:-3] + r")?\s*(?:-|to|and)\s*" + _NUM + r"\s*" + _UNIT, text, re.I)
    if between and re.search(r"\b(between|from|range)\b|\d\s*(-|to)\s*\d", text, re.I):
        lo_unit = between.group(2) or between.group(4)
        lo = _unit_value(between.group(1), lo_unit)
        hi = _unit_value(between.group(3), between.group(4))
        if lo < hi:
            return {"min": lo, "max": hi}
    for m in MONEY.finditer(text):
        if m.group(2).lower() in ("m", "k") and not re.search(r"budget|pkr|rs|price|under|below", text, re.I):
            continue  # avoid '10 m' style noise
        before = text[: m.start()]
        value = _unit_value(m.group(1), m.group(2))
        if re.search(r"down\s*payment|downpayment|advance|token|pay\s*now|upfront", text[max(0, m.start() - 30): m.end() + 25], re.I):
            continue  # that is a down payment, not a budget
        if MIN_WORDS.search(before):
            out["min"] = value
        else:
            out["max"] = value
        break
    return out


SIZE = re.compile(_NUM + r"\s*(marla|kanal)\b", re.I)


def parse_size_marla(text: str) -> float | None:
    m = SIZE.search(text)
    if not m:
        return None
    n = float(m.group(1))
    return n * 20 if m.group(2).lower() == "kanal" else n


TYPES = [
    ("commercial", r"\b(commercial|shop|plaza|office)\b"),
    ("apartment", r"\b(apartment|flat|apartments|flats)\b"),
    ("house", r"\b(house|home|houses|villa|bungalow|ghar)\b"),
    ("plot", r"\b(plot|plots|file|land)\b"),
]


def parse_property_type(text: str) -> str | None:
    for name, pat in TYPES:
        if re.search(pat, text, re.I):
            return name
    return None


AREA_ALIASES = {
    "bahria enclave": "Bahria Enclave", "bahria orchard": "Bahria Orchard",
    "bahria town": "Bahria Town", "bahria": "Bahria Town",
    "dha": "DHA", "defence": "DHA", "defense": "DHA",
    "gulberg greens": "Gulberg Greens", "gulberg": "Gulberg Greens",
    "b-17": "B-17", "b17": "B-17", "multi gardens": "B-17",
    "johar town": "Johar Town", "johar": "Johar Town",
}


def parse_area(text: str, known: list[str] | None = None) -> str | None:
    low = text.lower()
    for k in sorted(known or [], key=len, reverse=True):
        if k.lower() in low:
            return k
    for alias in sorted(AREA_ALIASES, key=len, reverse=True):
        if re.search(r"\b" + re.escape(alias) + r"\b", low):
            return AREA_ALIASES[alias]
    return None


INSTALLMENT = re.compile(r"\b(installments?|instalments?|qist|qisht|kist|monthly|easy plan|payment plan|on plan)\b", re.I)


def wants_installments(text: str) -> bool:
    return bool(INSTALLMENT.search(text))


def parse_timeline(text: str) -> str | None:
    t = text.lower()
    if re.search(r"\b(asap|urgent|immediately|right away|today|this week)\b", t):
        return "this week"
    if re.search(r"\b(this month|within (a|one) month|in a month)\b", t):
        return "this month"
    if re.search(r"\bnext month\b", t):
        return "next month"
    if wants_installments(t) and re.search(r"\d+\s*(months?|years?)\s*(plan|installments?)|over\s*\d+\s*months?", t):
        pass  # those months describe the payment plan, not the buying timeline
    else:
        m = re.search(r"\b(?:within|in|next|after)\s*(\d+)\s*(months?|weeks?)\b", t)
        if m:
            return f"{m.group(1)} {m.group(2)}"
    if re.search(r"\b(next year|later|just looking|exploring|browsing|not sure)\b", t):
        return "exploring"
    return None


PROPERTY_ID = re.compile(r"(?:#\s*|\b(?:plot|property|house|listing|id|no\.?|number|p-)\s*#?\s*)(\d{3,5})\b", re.I)


def parse_property_id(text: str) -> int | None:
    m = PROPERTY_ID.search(text)
    return int(m.group(1)) if m else None


def parse_down_payment_pct(text: str) -> float | None:
    m = re.search(r"(\d{1,2}(?:\.\d+)?)\s*%\s*(?:down|advance|upfront|now)?", text, re.I)
    if m and re.search(r"down|advance|upfront|pay|now", text, re.I):
        return float(m.group(1))
    return None


def parse_down_payment_amount(text: str) -> int | None:
    m = re.search(r"(?:down\s*payment|downpayment|advance|upfront)\D{0,15}" + _NUM + r"\s*" + _UNIT, text, re.I) or \
        re.search(_NUM + r"\s*" + _UNIT + r"\s*(?:as\s*)?(?:down\s*payment|downpayment|advance|upfront)", text, re.I)
    return _unit_value(m.group(1), m.group(2)) if m else None


def parse_months(text: str) -> int | None:
    m = re.search(r"(\d{1,3})\s*(?:-\s*)?months?", text, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{1,2})\s*(?:-\s*)?years?", text, re.I)
    if m and wants_installments(text):
        return int(m.group(1)) * 12
    return None


DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
TIME = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.I)


def parse_slot(text: str, today: datetime | None = None) -> str | None:
    """'Saturday 11am' -> 'YYYY-MM-DD 11:00' (next Saturday). Returns None without both a day and a time."""
    today = today or datetime.now()
    t = text.lower()
    date = None
    iso = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", t)
    if iso:
        date = datetime.strptime(iso.group(1), "%Y-%m-%d")
    elif "day after tomorrow" in t:
        date = today + timedelta(days=2)
    elif "tomorrow" in t:
        date = today + timedelta(days=1)
    elif "today" in t:
        date = today
    else:
        for i, d in enumerate(DAYS):
            if re.search(r"\b" + d[:3] + r"(" + d[3:] + r")?\b", t):
                ahead = (i - today.weekday()) % 7 or 7
                date = today + timedelta(days=ahead)
                break
    if not date:
        return None
    hour = minute = None
    for m in TIME.finditer(t):
        if m.group(3) or m.group(2):  # needs am/pm or hh:mm to count as a time
            hour, minute = int(m.group(1)), int(m.group(2) or 0)
            if m.group(3) and m.group(3).lower() == "pm" and hour < 12:
                hour += 12
            if m.group(3) and m.group(3).lower() == "am" and hour == 12:
                hour = 0
            break
    if hour is None:
        if "morning" in t:
            hour, minute = 11, 0
        elif "afternoon" in t:
            hour, minute = 14, 0
        elif "evening" in t:
            hour, minute = 16, 0
        else:
            return None
    return f"{date:%Y-%m-%d} {hour:02d}:{minute:02d}"


PHONE = re.compile(r"(?:\+92|0092|0)?\s?3\d{2}[\s-]?\d{7}\b")
CNIC = re.compile(r"\b\d{5}-?\d{7}-?\d\b")
NAME = re.compile(r"\b(?i:my name is|this is|i am|i'm|name:|main)\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)")


def parse_phone(text: str) -> str | None:
    m = PHONE.search(text)
    return re.sub(r"\s", "", m.group(0)) if m else None


def parse_name(text: str) -> str | None:
    m = NAME.search(text)
    if not m:
        return None
    name = m.group(1)
    if name.lower().split()[0] in {"looking", "interested", "planning", "searching", "not", "from", "a", "an", "the"}:
        return None
    return name


# Facts we must never write to long-term memory (canvas box 9)
PII_PATTERNS = [
    ("cnic", CNIC),
    ("phone", PHONE),
    ("income", re.compile(r"\b(salary|income|i earn|earning|net worth|bank balance)\b", re.I)),
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")),
]


def contains_pii(text: str) -> list[str]:
    return [name for name, pat in PII_PATTERNS if pat.search(text or "")]


def redact(text: str) -> str:
    text = CNIC.sub("[CNIC]", text)
    text = PHONE.sub("[PHONE]", text)
    return re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[EMAIL]", text)


def extract_profile(text: str, known_areas: list[str] | None = None) -> dict:
    """Everything the qualifier can read from one customer message."""
    budget = parse_budget(text)
    found = {
        "area": parse_area(text, known_areas),
        "size_marla": parse_size_marla(text),
        "property_type": parse_property_type(text),
        "budget_max": budget.get("max"),
        "budget_min": budget.get("min"),
        "timeline": parse_timeline(text),
        "wants_installments": True if wants_installments(text) else None,
        "property_id": parse_property_id(text),
        "name": parse_name(text),
        "phone": parse_phone(text),
    }
    return {k: v for k, v in found.items() if v is not None}
