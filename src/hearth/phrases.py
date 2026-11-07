"""Fixed messages for the parent, used when speed or safety matters more than
phrasing (emergencies, scams, LLM outages) and for routine check-ins.

Two styles: "kanglish" (Kannada written in English letters, mixed with
English, the way many families in Karnataka text) and "english".
Have a native speaker in the family read these before real use.
"""

from __future__ import annotations

import random

_PHRASES: dict[str, dict[str, list[str]]] = {
    "kanglish": {
        "emergency": [
            "{call}, please eega takshana {ambulance} ge call maadi for an ambulance, illa andre "
            "pakkada maneyavarannu kugi. Naanu {names} ge eega ne call maadtiddini. "
            "Alle iri, phone hattira ittkoli. 🙏",
        ],
        "scam": [
            "{call}, idu scam. Police, CBI, customs yaaru video call alli arrest maadalla. "
            "Duddu kalisbedi, OTP kodbedi, yaarigu bank details kodbedi. Call cut maadi. "
            "Naanu {name} ge eega tilistiddini, avaru nimge call maadtare.",
        ],
        "money": [
            "{call}, yaaru duddu, OTP athava Aadhaar kelidru kodbedi. {name} jothe maathaadi "
            "aamele nodona. Naanu {name} ge tilistiddini.",
        ],
        "received": [
            "{call}, nim message bantu. {name} ge tilisiddini. 🙏",
        ],
        "photo": [
            "{call}, photo bantu, thanks. Idanna {name} ge kalisiddini, avaru doctor jothe check maadtare.",
        ],
        "family_calling": [
            "{call}, {name} nimge eega call maadtare. 🙏",
        ],
        "morning": [
            "Namaskara {call}! 🌞 Hegiddira? Raatri nidde chennagi aaytha?",
            "Shubhodaya {call}! Tiffin aaytha? Ivattu hegide nimge?",
        ],
        "evening": [
            "{call}, sanje aaytu. Ivattu dina hegittu?",
            "{call}, oota aaytha? Raatri maatre time ge tagolli, aaytha?",
        ],
        "handover": [
            "{call}, naanu Hearth, ille iddini. Enu beku andre heli.",
        ],
        "quiet": [
            "{call}, kaafi aaytha? Ivattu nim message bandilla, hegiddira? 🙂",
        ],
        "relative": [
            "{call}, yaaru enu helidru duddu kalisbedi, OTP kodbedi. Idu scam aagirabahudu. "
            "Naanu {names} jothe eega check maadtiddini. Call cut maadi.",
        ],
        "relative_seen": [
            "{call}, {name} {minutes} nimisha munche namma family chat alli maathaadidru. "
            "Aa call fake ansutte. Duddu kalisbedi. {name} ge nimge call maadoke heltiddini.",
        ],
        "relative_safe": [
            "{call}, {name} chennagiddare, safe aagiddare antha eega ne heliddare. "
            "Aa call fake. Duddu kalisbedi. 🙏",
        ],
        "voice_failed": [
            "{call}, nim voice message bantu, aadre nanage sariyaagi kelisalilla. "
            "{name} ge kalisiddini, avaru keltaare. Saadhya aadre type maadi heli.",
        ],
    },
    "english": {
        "emergency": [
            "{call}, please call {ambulance} for an ambulance right now, or call out to a neighbour. "
            "I'm calling {names} this minute. Stay where you are and keep the phone close. 🙏",
        ],
        "scam": [
            "{call}, this is a scam. No real police or CBI officer arrests anyone on a video call. "
            "Don't send money, don't share any OTP or bank details. Please hang up. "
            "I'm telling {name} right now and they will call you.",
        ],
        "money": [
            "{call}, please don't give money, an OTP or your Aadhaar to anyone who asks. "
            "Let's check with {name} first. I'm telling {name} now.",
        ],
        "received": [
            "{call}, I got your message and I've shared it with {name}. 🙏",
        ],
        "photo": [
            "{call}, thank you for the photo. I've sent it to {name} so they can check it with the doctor.",
        ],
        "family_calling": [
            "{call}, {name} is calling you now. 🙏",
        ],
        "morning": [
            "Good morning {call}! 🌞 How are you today? Did you sleep well?",
            "Good morning {call}! Had your breakfast? How are you feeling?",
        ],
        "evening": [
            "Good evening {call}. How was your day?",
            "{call}, have you had dinner? Don't forget your night tablets.",
        ],
        "handover": [
            "{call}, it's Hearth, I'm here. Tell me if you need anything.",
        ],
        "quiet": [
            "{call}, had your coffee? I haven't heard from you today. How are you? 🙂",
        ],
        "relative": [
            "{call}, whatever the caller says, please don't send money or share an OTP. This is "
            "probably a scam. I'm checking with {names} right now. Please hang up.",
        ],
        "relative_seen": [
            "{call}, {name} was chatting with the family {minutes} minutes ago. That call sounds fake. "
            "Don't send any money. I'm asking {name} to call you.",
        ],
        "relative_safe": [
            "{call}, {name} just told us they are fine and safe. That call was fake. "
            "Please don't send any money. 🙏",
        ],
        "voice_failed": [
            "{call}, I got your voice message but couldn't hear it clearly. I've sent it to {name} "
            "to listen. If you can, please type it too.",
        ],
    },
}


def phrase(style: str, key: str, **values: str) -> str:
    table = _PHRASES.get(style) or _PHRASES["english"]
    options = table.get(key) or _PHRASES["english"][key]
    return random.choice(options).format(**values)
