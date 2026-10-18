import pytest

from atlas.normalize import norm_title, split_edition, year_of


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("The Witcher 3: Wild Hunt", "Witcher 3 Wild Hunt"),
        ("Hades: Definitive Edition", "Hades"),
        ("DOOM™", "doom"),
        ("Café Owner Simulator", "Cafe Owner Simulator"),
        ("Final Fantasy VII", "Final Fantasy 7"),
        ("Tom & Jerry", "Tom and Jerry"),
        ("Portal 2 (Steam)", "Portal 2"),
        ("snake_game", "snake game"),
    ],
)
def test_equivalent_titles(a, b):
    assert norm_title(a) == norm_title(b)


def test_sequels_stay_distinct():
    assert norm_title("Resident Evil 2") != norm_title("Resident Evil 3")


def test_split_edition():
    assert split_edition("Hades: Definitive Edition") == ("Hades", "Definitive Edition")
    assert split_edition("Hades") == ("Hades", None)


def test_year_of():
    assert year_of("2020-09-17") == 2020
    assert year_of(None) is None
    assert year_of("n/a") is None
