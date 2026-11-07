"""Deterministic triage of a parent's message.

This runs before and independently of the LLM. If the model is down, slow or
wrong, a message about chest pain or a "CBI officer" asking for money still
pages the family. The LLM can raise a severity, never lower one set here.

Terms cover English, Kannada (script and Kanglish spellings) and Hindi
(Devanagari and Hinglish), because that is how families in India text.
None of this is medical advice: it decides only how fast a person is told.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import IntEnum


class Severity(IntEnum):
    NONE = 0
    LOW = 1      # log it, mention it in the digest
    MEDIUM = 2   # tell the person on duty, no rush
    HIGH = 3     # page the person on duty now, re-page if no ack
    RED = 4      # possible emergency: page everyone at once

    @property
    def label(self) -> str:
        return self.name.lower()

    @classmethod
    def parse(cls, value: str | int | Severity | None) -> Severity:
        if isinstance(value, Severity):
            return value
        if isinstance(value, int):
            return cls(max(0, min(4, value)))
        text = str(value or "none").strip().lower()
        return {s.label: s for s in cls}.get(text, cls.NONE)


@dataclass
class Assessment:
    severity: Severity = Severity.NONE
    kinds: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    # Family members the message is about ("they detained your son Shamanth").
    about: list[str] = field(default_factory=list)
    # Readings she reported, as written for people: "BP 128/82", "sugar 120".
    readings: list[str] = field(default_factory=list)
    # She said she took her medicines.
    took_medicine: bool = False

    def raise_to(self, severity: Severity, kind: str, reason: str) -> None:
        if kind not in self.kinds:
            self.kinds.append(kind)
        self.reasons.append(reason)
        if severity > self.severity:
            self.severity = severity

    @property
    def primary_kind(self) -> str:
        """The kind that set the severity, for the alert headline."""
        order = ["medical-emergency", "fall", "relative-in-trouble", "scam", "money-request", "vitals",
                 "missed-medicine", "unwell", "wellbeing", "pain"]
        for kind in order:
            if kind in self.kinds:
                return kind
        return self.kinds[0] if self.kinds else "update"


def _latin(*terms: str) -> list[re.Pattern[str]]:
    """Whole-word patterns for Latin-script terms (English, Kanglish, Hinglish)."""
    return [re.compile(r"(?<![a-z])" + re.escape(t) + r"(?![a-z])") for t in terms]


def _indic(*terms: str) -> list[str]:
    """Substring terms for Kannada and Devanagari script."""
    return list(terms)


# ── Possible emergencies (RED) ───────────────────────────────────────────────

_EMERGENCY_LATIN = _latin(
    "chest pain", "chest is paining", "pain in chest", "pain in my chest", "heart attack",
    "can't breathe", "cant breathe", "cannot breathe", "not able to breathe", "breathless",
    "short of breath", "difficulty breathing", "slurred", "face drooping", "face is drooping",
    "one side weak", "cannot move my arm", "can't move my arm", "fainted", "unconscious",
    "passed out", "blacked out", "vomiting blood", "blood in vomit", "seizure",
    "ede novu", "ede noovu", "edhe novu", "usiru kattide", "usiru agta illa", "usiru aagtilla",
    "mukha sotta", "seene mein dard", "seene me dard", "saans nahi", "saans nahin", "behosh",
)
_EMERGENCY_INDIC = _indic(
    "ಎದೆ ನೋವು", "ಉಸಿರು ಕಟ್ಟ", "ಉಸಿರಾಡಲು", "ಮುಖ ಸೊಟ್ಟ", "ಪ್ರಜ್ಞೆ ತಪ್ಪ",
    "सीने में दर्द", "सांस नहीं", "साँस नहीं", "बेहोश",
)

_FALL = [
    re.compile(r"(?<![a-z])i fell(?! asleep)(?![a-z])"),
    re.compile(r"(?<![a-z])(fell down|had a fall|slipped and fell|fell in the|fell on the)(?![a-z])"),
    re.compile(r"(?<![a-z])(bidde|biddbitte|bidd?u bitte|jaari bidde|gir gayi|gir gaya|gir gai)(?![a-z])"),
]
_FALL_INDIC = _indic("ಬಿದ್ದೆ", "ಬಿದ್ದು", "ಜಾರಿ ಬಿದ್ದ", "गिर गई", "गिर गया", "गिर पड़")

# ── Scams (HIGH) ─────────────────────────────────────────────────────────────

_SCAM_STRONG = _latin(
    "digital arrest", "money laundering", "otp", "kyc", "anydesk", "any desk", "teamviewer",
    "quicksupport", "screen share", "account blocked", "account will be blocked",
    "sim will be blocked", "sim blocked", "drugs in your parcel", "drugs in parcel",
    "parcel seized", "electricity will be cut", "power will be cut", "lottery", "you have won",
    "gift card", "verification amount", "security deposit", "refund amount",
)
_SCAM_STRONG_INDIC = _indic("ಓಟಿಪಿ", "ಡಿಜಿಟಲ್ ಅರೆಸ್ಟ್", "ओटीपी", "डिजिटल अरेस्ट")

_AUTHORITY = _latin(
    "cbi", "police", "policeman", "ed officer", "enforcement directorate", "customs", "narcotics",
    "ncb", "trai", "cyber crime", "cybercrime", "crime branch", "income tax", "rbi", "court",
    "judge", "arrest", "warrant", "fedex", "courier", "bank manager", "bank officer",
)
_AUTHORITY_INDIC = _indic("ಪೊಲೀಸ್", "ಕೋರ್ಟ್", "पुलिस", "कोर्ट", "गिरफ्तार")

_MONEY = _latin(
    "transfer", "send money", "pay", "payment", "upi", "gpay", "phonepe", "paytm", "rupees",
    "lakh", "lakhs", "rs", "bank details", "account number", "card number", "cvv", "pin",
    "aadhaar", "aadhar", "pan card", "video call", "stay on the call", "don't tell anyone",
    "dont tell anyone", "hana kalisu", "duddu kalisu", "paise bhejo", "paisa bhejo",
)
_MONEY_INDIC = _indic("ಹಣ", "ದುಡ್ಡು", "ಆಧಾರ್", "पैसे", "पैसा", "आधार", "₹")

# ── Concerns (MEDIUM) ────────────────────────────────────────────────────────

_MISSED_MEDICINE = _latin(
    "forgot my tablet", "forgot tablet", "forgot the tablet", "forgot medicine",
    "forgot my medicine", "didn't take my tablet", "didnt take my tablet", "missed my tablet",
    "missed my medicine", "medicine finished", "tablets finished", "ran out of",
    "tablet mugidide", "maatre mugidide", "maatre illa", "maatre tagondilla", "tablet tagondilla",
    "dawai khatam", "dawa khatam",
)
_MISSED_MEDICINE_INDIC = _indic("ಮಾತ್ರೆ ಮುಗಿ", "ಮಾತ್ರೆ ತಗೊಂಡಿಲ್ಲ", "ಮಾತ್ರೆ ಇಲ್ಲ", "दवाई खत्म", "दवा खत्म")

_UNWELL = _latin(
    "dizzy", "giddy", "giddiness", "fever", "vomiting", "vomited", "loose motion", "diarrhoea",
    "diarrhea", "very weak", "not eating", "haven't eaten", "havent eaten", "no appetite",
    "severe pain", "unbearable", "tumba novu", "talesuttu", "tale suttu", "jwara", "oota madilla",
    "chakkar", "bukhar", "ulti",
)
_UNWELL_INDIC = _indic("ತಲೆ ಸುತ್ತು", "ಜ್ವರ", "ವಾಂತಿ", "ಊಟ ಮಾಡಿಲ್ಲ", "ತುಂಬಾ ನೋವು", "चक्कर", "बुखार", "उल्टी")

# ── Things to log (LOW) ──────────────────────────────────────────────────────

_PAIN = _latin("pain", "paining", "hurts", "ache", "aching", "novu", "dard")
_PAIN_INDIC = _indic("ನೋವು", "दर्द")
_WELLBEING = _latin(
    "lonely", "alone", "sad", "bored", "couldn't sleep", "could not sleep", "no sleep",
    "bejaar", "bejaaru", "nidde barlilla", "akela", "akeli", "udaas",
)
_WELLBEING_INDIC = _indic("ಬೇಜಾರು", "ನಿದ್ದೆ ಬರಲಿಲ್ಲ", "ಒಬ್ಬಳೇ", "अकेली", "उदास")

_STRANGER = re.compile(
    r"(?<![a-z])(someone|somebody|unknown number|stranger|a man called|a lady called|caller"
    r"|they asked|asked me to|yaaro|yaro|koi aadmi|koi aurat)(?![a-z])"
    r"|ಯಾರೋ|ಅಪರಿಚಿತ|कोई आदमी|अनजान"
)

# ── "Your son has been detained": a relative in trouble (HIGH) ───────────────

_RELATIVE = _latin(
    "son", "daughter", "grandson", "granddaughter", "your child", "your children", "your boy",
    "your girl", "maga", "magalu", "magane", "makkalu", "mommaga", "mommagalu", "beta", "beti", "bete",
)
_RELATIVE_INDIC = _indic("ಮಗ", "ಮಗಳು", "ಮಕ್ಕಳು", "ಮೊಮ್ಮಗ", "बेटा", "बेटी", "बच्चे")
_TROUBLE = _latin(
    "detained", "arrested", "arrest", "custody", "kidnapped", "kidnap", "accident", "hospital",
    "police station", "jail", "lock up", "lockup", "case filed", "caught with", "in trouble", "hostage",
    "don't call", "dont call", "do not call",
)
_TROUBLE_INDIC = _indic("ಅಪಘಾತ", "ಆಸ್ಪತ್ರೆ", "ಅರೆಸ್ಟ್", "ಪೊಲೀಸ್ ಸ್ಟೇಷನ್", "ಜೈಲ್", "गिरफ्तार", "एक्सीडेंट", "अस्पताल", "जेल")

_CALL_CONTEXT = re.compile(
    r"(?<![a-z])(they said|he said|she said|called me|got a call|a call came|phone call|video call"
    r"|call bantu|phone bantu|call maadidru|helidru|heltidaare|kisi ne call)(?![a-z])"
    r"|ಕಾಲ್ ಬಂತು|ಫೋನ್ ಬಂತು|ಹೇಳಿದ್ರು|फोन आया|कॉल आया"
)

# ── She took her medicines (for the shift handoff) ───────────────────────────

_TOOK_MEDICINE = re.compile(
    r"(?<![a-z])(took (my |the |all )?(tablets?|medicines?|pills?)|had (my )?(tablets?|medicines?)"
    r"|(tablet|maatre|maathre) tagonde|(tablet|maatre|maathre) tagondidini|(tablet|maatre) aaytu)(?![a-z])"
    r"|ಮಾತ್ರೆ ತಗೊಂಡೆ|दवाई ले ली|दवा ले ली"
)

_BP = re.compile(r"(?:bp|b\.p\.|pressure)\D{0,12}(\d{2,3})\s*/\s*(\d{2,3})")
_SUGAR = re.compile(r"(?:sugar|glucose)\D{0,12}(\d{2,3})")


def normalise(text: str) -> str:
    text = text.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    return re.sub(r"\s+", " ", text.lower()).strip()


def _hits(text: str, latin: list[re.Pattern[str]], indic: list[str] | None = None) -> list[str]:
    found = [p.pattern for p in latin if p.search(text)]
    found += [term for term in (indic or []) if term in text]
    return found


def _clean(pattern: str) -> str:
    """Turn a compiled whole-word pattern back into the term, for reasons."""
    return re.sub(r"\(\?<!\[a-z\]\)|\(\?!\[a-z\]\)|\\", "", pattern)


def assess(
    text: str,
    *,
    family_names: Iterable[str] = (),
    learned_phrases: Iterable[str] = (),
) -> Assessment:
    """Classify a message. Pure and fast; safe to call on every message.

    ``family_names`` are the children's names, so "they detained Shamanth" is
    caught. ``learned_phrases`` are scam phrases the family confirmed before
    (the "Seen in this family" part of hearth-scam-patterns).
    """
    t = normalise(text)
    result = Assessment()
    if not t:
        return result

    named = [name for name in family_names if name and re.search(
        r"(?<![a-z])" + re.escape(name.lower()) + r"(?![a-z])", t)]
    relative = named or _hits(t, _RELATIVE, _RELATIVE_INDIC)
    trouble = _hits(t, _TROUBLE, _TROUBLE_INDIC)
    # Only with a caller in the story: "Shreya took me to hospital" is not a scam.
    caller = (
        _STRANGER.search(t) or _CALL_CONTEXT.search(t)
        or _hits(t, _AUTHORITY, _AUTHORITY_INDIC) or _hits(t, _MONEY, _MONEY_INDIC)
    )
    if relative and trouble and caller:
        who = ", ".join(named) if named else "one of her children"
        result.about = named
        result.raise_to(
            Severity.HIGH, "relative-in-trouble",
            f"a caller says {who} is in trouble ({_clean(trouble[0])}): a common scam opener",
        )

    for phrase_ in learned_phrases:
        if len(phrase_.strip()) >= 4 and normalise(phrase_) in t:
            result.raise_to(Severity.HIGH, "scam", f"matches a scam this family saw before: \"{phrase_}\"")
            break

    if _TOOK_MEDICINE.search(t):
        result.took_medicine = True

    if hits := _hits(t, _EMERGENCY_LATIN, _EMERGENCY_INDIC):
        result.raise_to(Severity.RED, "medical-emergency", f"possible emergency: {_clean(hits[0])}")
    if any(p.search(t) for p in _FALL) or any(term in t for term in _FALL_INDIC):
        result.raise_to(Severity.RED, "fall", "she may have fallen")

    strong = _hits(t, _SCAM_STRONG, _SCAM_STRONG_INDIC)
    authority = _hits(t, _AUTHORITY, _AUTHORITY_INDIC)
    money = _hits(t, _MONEY, _MONEY_INDIC)
    if strong:
        result.raise_to(Severity.HIGH, "scam", f"known scam pattern: {_clean(strong[0])}")
    elif authority and money:
        result.raise_to(
            Severity.HIGH, "scam",
            f"someone claiming authority ({_clean(authority[0])}) and money or ID ({_clean(money[0])})",
        )
    elif money and _STRANGER.search(t):
        result.raise_to(Severity.MEDIUM, "money-request", "a stranger asked about money or ID")

    for match in _BP.finditer(t):
        systolic, diastolic = int(match.group(1)), int(match.group(2))
        result.readings.append(f"BP {systolic}/{diastolic}")
        if systolic >= 180 or diastolic >= 120:
            result.raise_to(Severity.RED, "vitals", f"BP {systolic}/{diastolic} is very high")
        elif systolic >= 160 or diastolic >= 100 or systolic < 90:
            result.raise_to(Severity.MEDIUM, "vitals", f"BP {systolic}/{diastolic} is outside her usual range")
        else:
            result.raise_to(Severity.LOW, "vitals", f"BP {systolic}/{diastolic}")
    for match in _SUGAR.finditer(t):
        sugar = int(match.group(1))
        result.readings.append(f"sugar {sugar}")
        if sugar < 70 or sugar > 300:
            result.raise_to(Severity.HIGH, "vitals", f"sugar {sugar} is far outside the usual range")
        else:
            result.raise_to(Severity.LOW, "vitals", f"sugar {sugar}")

    if _hits(t, _MISSED_MEDICINE, _MISSED_MEDICINE_INDIC):
        result.raise_to(Severity.MEDIUM, "missed-medicine", "a missed dose or medicine running out")
    if hits := _hits(t, _UNWELL, _UNWELL_INDIC):
        result.raise_to(Severity.MEDIUM, "unwell", f"feeling unwell: {_clean(hits[0])}")
    if _hits(t, _PAIN, _PAIN_INDIC):
        result.raise_to(Severity.LOW, "pain", "mentioned pain")
    if _hits(t, _WELLBEING, _WELLBEING_INDIC):
        result.raise_to(Severity.LOW, "wellbeing", "low mood, loneliness or poor sleep")

    return result
