import pytest

from app import nlu
from app.clock import FixedClock

NOW = FixedClock("2026-11-13T16:00:00+05:30").now()


@pytest.mark.parametrize(
    "text,want",
    [
        ("table for four at 7:30", 4),
        ("party of 6", 6),
        ("3 people tomorrow", 3),
        ("a couple", 2),
        ("book for three", 3),
        ("table for 2 tonight at 8", 2),
        ("for 8 pm", None),
        ("hello", None),
    ],
)
def test_party_size(text, want):
    assert nlu.find_party_size(text) == want


@pytest.mark.parametrize(
    "text,want",
    [
        ("7:30", "19:30"),
        ("7:30 pm", "19:30"),
        ("7.30", "19:30"),
        ("half past seven", "19:30"),
        ("seven thirty", "19:30"),
        ("at eight", "20:00"),
        ("1 PM", "13:00"),
        ("noon", "12:00"),
        ("19:30", "19:30"),
        ("table for 2 tonight at 8", "20:00"),
        ("quarter to nine", "20:45"),
        ("11:30 am", "11:30"),
    ],
)
def test_time(text, want):
    assert nlu.parse_time(text) == want


@pytest.mark.parametrize(
    "text,want",
    [
        ("tonight", "2026-11-13"),
        ("tomorrow", "2026-11-14"),
        ("saturday", "2026-11-14"),
        ("next friday", "2026-11-20"),
        ("friday", "2026-11-13"),
        ("14th november", "2026-11-14"),
        ("november 20", "2026-11-20"),
        ("2026-12-01", "2026-12-01"),
        ("day after tomorrow", "2026-11-15"),
    ],
)
def test_date(text, want):
    assert nlu.parse_date(text, NOW) == want


def test_invalid_date_is_none():
    assert nlu.parse_date("february 30", NOW) is None


@pytest.mark.parametrize("text", ["TV-D4F8", "tv d4f8", "T V D 4 F 8", "TV D four F eight", "code is TVD4F8."])
def test_code(text):
    assert nlu.normalize_code(text) == "TV-D4F8"


def test_names_and_phone():
    assert nlu.find_name("Hi, my name is Priya Sharma") == "Priya Sharma"
    assert nlu.find_name("It's Ananya Rao, please cancel") == "Ananya Rao"
    assert nlu.find_name("I am allergic to nuts") is None
    assert nlu.normalize_phone("98765 43210") == "+919876543210"
