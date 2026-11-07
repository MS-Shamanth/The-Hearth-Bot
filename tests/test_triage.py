import pytest

from hearth.triage import Severity, assess


@pytest.mark.parametrize(
    "text",
    [
        "I have chest pain since morning",
        "ede novu aagtide",
        "ಎದೆ ನೋವು ಇದೆ",
        "I fell in the bathroom",
        "bathroom alli jaari bidde",
        "seene mein dard ho raha hai",
        "can't breathe properly",
        "BP 190/110 now",
    ],
)
def test_possible_emergencies_are_red(text):
    assert assess(text).severity == Severity.RED


@pytest.mark.parametrize(
    "text",
    [
        "A CBI officer called. My Aadhaar is in a money laundering case. I must transfer 2 lakh for verification.",
        "Police video call maadidru, arrest maadtini antha heltidaare, duddu kalisu antha",
        "bank inda call bantu, OTP kelidru",
        "They said my account will be blocked, update KYC",
        "someone asked me to install anydesk",
        "FedEx parcel has drugs, customs officer wants payment",
    ],
)
def test_scams_are_high(text):
    result = assess(text)
    assert result.severity == Severity.HIGH
    assert "scam" in result.kinds


@pytest.mark.parametrize(
    "text, kind",
    [
        ("forgot my tablet last night", "missed-medicine"),
        ("maatre mugidide", "missed-medicine"),
        ("feeling dizzy since lunch", "unwell"),
        ("talesuttu barta ide", "unwell"),
        ("BP 165/95", "vitals"),
    ],
)
def test_concerns_are_medium(text, kind):
    result = assess(text)
    assert result.severity == Severity.MEDIUM
    assert kind in result.kinds


@pytest.mark.parametrize(
    "text",
    ["Slept okay, knee hurts again", "feeling a bit lonely today", "BP 138/86"],
)
def test_things_to_log_are_low(text):
    assert assess(text).severity == Severity.LOW


@pytest.mark.parametrize(
    "text",
    [
        "Good morning! Had idli and coffee",
        "I fell asleep watching TV",
        "the milkman came, I'll pay him tomorrow",
        "Shreya called, so happy",
        "",
    ],
)
def test_everyday_messages_do_not_page(text):
    assert assess(text).severity <= Severity.LOW


def test_primary_kind_prefers_the_most_serious():
    result = assess("I fell and now someone from police called asking for OTP")
    assert result.severity == Severity.RED
    assert result.primary_kind == "fall"


FAMILY = ["Shamanth", "Shreya"]


@pytest.mark.parametrize(
    "text, about",
    [
        ("A police officer called, they said Shamanth is arrested and I must not call him", ["Shamanth"]),
        ("someone called saying your son had an accident, send money for the hospital", []),
        ("call bantu, Shreya accident aagide antha helidru, duddu kalisu antha", ["Shreya"]),
    ],
)
def test_relative_in_trouble_is_high(text, about):
    result = assess(text, family_names=FAMILY)
    assert result.severity == Severity.HIGH
    assert result.primary_kind == "relative-in-trouble"
    assert result.about == about


def test_family_news_without_a_caller_is_not_the_scam():
    result = assess("Shreya took me to the hospital for my checkup", family_names=FAMILY)
    assert "relative-in-trouble" not in result.kinds


def test_learned_phrases_are_high():
    text = "SMS says electricity will be disconnected tonight at 9.30, call this number"
    assert assess(text).severity < Severity.HIGH
    result = assess(text, learned_phrases=["electricity will be disconnected tonight"])
    assert result.severity == Severity.HIGH and "scam" in result.kinds


def test_readings_and_medicines_are_collected():
    result = assess("BP 128/82 this morning, maatre tagonde")
    assert result.readings == ["BP 128/82"]
    assert result.took_medicine
