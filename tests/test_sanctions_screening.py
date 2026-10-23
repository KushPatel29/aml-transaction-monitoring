"""
Name screening against the Consolidated Canadian Autonomous Sanctions List.

The real list is downloaded at run time and never committed (no real person may
appear in this repository), so these tests run against a fictional list in the
same XML schema. They pin what a screening desk needs to be true: every listed
name finds itself, the spellings a customer actually types still find it, a
namesake with a different given name and birth year does not, and the
evaluation chooses its threshold on one half of the list and reports on the
other without ever writing a listed name.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import sanctions_screening as S

FIXTURE = Path(__file__).parent / "fixtures" / "sema_fictional.xml"


@pytest.fixture(scope="module")
def parties():
    return S.parse_list(FIXTURE.read_bytes())


@pytest.fixture(scope="module")
def index(parties):
    return S.Index.build(parties)


def test_the_published_schema_parses_into_people_entities_and_ships(parties):
    kinds = {p.kind for p in parties}
    assert kinds == {"individual", "entity", "ship"}
    assert len(parties) == 7
    first = parties[0]
    assert first.names[0] == "Radomir Yevgenyevich Zarkhanov"
    assert len(first.names) == 3 and any("Зарханов" in n for n in first.names)
    assert first.birth_year == 1966
    assert parties[1].birth_year == 1979  # a bare year parses too
    ship = next(p for p in parties if p.kind == "ship")
    assert ship.birth_year is None  # a build date is not a birth year


def test_scripts_are_told_apart():
    assert S.script_of("Галина Лукашенко") == "cyrillic"
    assert S.script_of("Łukašenka Halina") == "latin"
    assert S.script_of("奥克萨娜") == "other"
    assert S.script_of("علی رضایی") == "other"


def test_a_persons_comma_separates_aliases_but_an_entitys_does_not(parties):
    bohdan = parties[2]
    assert "Bohdan Ivashchenko" in bohdan.names
    assert "Bogdan Nikolayevich Ivashenko" in bohdan.names
    entity = next(p for p in parties if p.kind == "entity")
    assert "Nordvind, Fiktiv and Partners" in entity.names


def test_an_alias_in_another_script_is_counted_not_silently_matched(parties, index):
    meta = S.describe(FIXTURE.read_bytes(), parties)
    assert meta["names_not_screened_other_script"] == 1
    assert meta["aliases_by_script"]["other"] == 1
    assert all(S.script_of(name) != "other" for _, name, _ in index.names)


def test_a_name_made_only_of_short_tokens_still_finds_itself(index):
    top = S.screen(index, "Ru Qo", top=1)[0]
    assert top["score"] >= 0.99 and top["party"].startswith("1, Part 1|2|Elsewhere")


def test_or_inside_an_alias_is_not_part_of_the_name(index):
    top = S.screen(index, "Oxana Velichkova", top=1)[0]
    assert top["score"] >= 0.945, top


def test_the_same_item_number_in_two_regimes_stays_two_parties(parties):
    keys = [p.key for p in parties]
    assert len(set(keys)) == len(keys)


def test_cyrillic_is_romanised_and_accents_dropped():
    assert S.normalize("Радомир Евгеньевич") == "radomir evgenevich"
    assert S.normalize("Богдан Іващенко") == "bogdan ivashchenko"
    assert S.normalize("HÉLÈNE Moreau-Quill") == "helene moreau quill"


def test_romanisation_variants_fold_to_one_key():
    assert S.fold("aleksandr") == S.fold("alexandr")
    assert S.fold("yevgeniy") == S.fold("evgeny")
    assert S.fold("khodorkovsky") == S.fold("hodorkovskiy")
    assert S.fold("ivashchenko") == S.fold("ivashenko")


def test_legal_forms_are_not_name_tokens():
    assert S.tokens("OOO Nordvind Fiktiv") == ("nordvind", "fiktiv")
    assert S.tokens("Northwind Fictional Trading LLC") == ("northwind", "fictional", "trading")


def test_every_listed_name_finds_its_own_party(parties, index):
    for party in parties:
        for name in party.names:
            if S.script_of(name) == "other":
                continue
            top = S.screen(index, name, top=1)[0]
            assert top["party"] == party.key and top["score"] >= 0.99, (name, top)


@pytest.mark.parametrize(
    ("typed", "item"),
    [
        ("Zarkhanov, Radomir", "1"),  # surname first, patronymic dropped
        ("RADOMIR ZARHANOV", "1"),  # another romanisation, upper case
        ("Radomir Zarkhanoff", "1"),  # a listed alias
        ("Радомир Зарханов", "1"),  # Cyrillic, as typed by the customer
        ("Oxana Velichkova", "2"),  # ks/x
        ("Bogdan Ivashenko", "3"),  # Ukrainian romanised the Russian way
        ("Helene Moreau Quill", "1"),  # accents and hyphen gone
        ("Nordvind Fiktiv", "1"),  # an entity alias without its legal form
    ],
)
def test_the_spellings_a_customer_types_still_alert(index, typed, item):
    top = S.screen(index, typed, top=1)[0]
    assert top["party"].split("|")[1] == item
    assert top["score"] >= 0.85, (typed, top)


def test_a_namesake_with_another_given_name_does_not_alert(index):
    top = (S.screen(index, "Svetlana Zarkhanova", top=1) or [{"score": 0}])[0]
    assert top["score"] < 0.85


def test_a_birth_year_that_disagrees_discounts_the_match(index):
    same = S.screen(index, "Radomir Zarkhanov", birth_year=1966, top=1)[0]["score"]
    other = S.screen(index, "Radomir Zarkhanov", birth_year=1994, top=1)[0]["score"]
    unknown = S.screen(index, "Radomir Zarkhanov", top=1)[0]["score"]
    assert same >= unknown > other


def test_variants_are_built_only_where_they_apply(parties):
    import random

    individual = S.variants(parties[0], random.Random(1))
    expected = {"exact", "reordered", "dropped_middle_name", "latin_alias", "cyrillic_alias"}
    assert expected <= set(individual)
    ship = S.variants(next(p for p in parties if p.kind == "ship"), random.Random(1))
    assert "dropped_middle_name" not in ship and "initial_for_given_name" not in ship


def test_the_split_is_stable_and_uses_both_halves(parties):
    assert [S.split_of(p) for p in parties] == [S.split_of(p) for p in parties]
    assert {S.split_of(p) for p in parties} == {"dev", "test"}


def test_synthetic_customers_are_reproducible():
    assert S.negatives(50, 7) == S.negatives(50, 7)
    assert S.negatives(50, 7) != S.negatives(50, 8)


def test_hard_negatives_are_never_a_listed_name(parties):
    listed = {" ".join(sorted(S.tokens(n))) for p in parties for n in p.names}
    for name, _ in S.hard_negatives(parties, 40, 3):
        assert " ".join(sorted(S.tokens(name))) not in listed


@pytest.fixture(scope="module")
def evaluation(parties):
    original = (S.NEGATIVES_PER_SPLIT, S.HARD_NEGATIVES_PER_SPLIT)
    S.NEGATIVES_PER_SPLIT, S.HARD_NEGATIVES_PER_SPLIT = 150, 30
    try:
        yield S.evaluate(parties)
    finally:
        S.NEGATIVES_PER_SPLIT, S.HARD_NEGATIVES_PER_SPLIT = original


def test_the_threshold_comes_from_the_development_half(evaluation):
    assert evaluation["rule"]["threshold_chosen_on"] == "dev"
    assert 0.70 <= evaluation["threshold"] <= 1.0
    assert evaluation["dev"]["standard_recall"] >= S.TARGET_RECALL
    assert set(evaluation["test"]) >= {"variants", "standard_recall", "alerts_per_10k_customers"}


def test_the_result_never_contains_a_listed_name(parties, evaluation):
    text = json.dumps(evaluation, default=dict).lower()
    for party in parties:
        for name in party.names:
            for token in S.tokens(name):
                if len(token) >= 5:
                    assert token not in text, token


def test_the_readme_block_is_written_from_the_result_alone(tmp_path, parties, evaluation):
    result = {
        "retrieved_at": "2026-10-23T00:00:00+00:00",
        "list": S.describe(FIXTURE.read_bytes(), parties),
        "evaluation": evaluation,
    }
    readme = tmp_path / "README.md"
    readme.write_text(f"before\n{S.BEGIN}\nSTALE-BLOCK\n{S.END}\nafter\n", encoding="utf-8")
    assert S.write_readme(result, readme)
    text = readme.read_text(encoding="utf-8")
    assert text.startswith("before\n") and text.endswith("\nafter\n") and "STALE-BLOCK" not in text
    assert "per 10,000" in text and "held-out half" in text
    assert "has not been screened" in S.readme_block({})
