"""
Name screening against Canada's Consolidated Autonomous Sanctions List.

    python -m src.sanctions_screening --download --evaluate --write-readme

The one part of this repository that uses a real list. Everything else is
Faker-generated, and the repository's rule is that no real person appears in
it, so the list is **downloaded at run time and never committed**. The
evaluation writes only aggregates (counts, recall, alert rates and the list's
checksum), never a listed name. The tests use a fictional list in the same XML
schema.

What it does, and what it measures:

* **Parse** the Global Affairs Canada XML: individuals, entities and ships, with
  aliases (some in Cyrillic), dates of birth and listing dates.
* **Normalise** every name the way a screening system has to: transliterate
  Cyrillic, strip accents and punctuation, drop legal forms (LLC, JSC, OOO...),
  and fold the spellings that romanisation varies (kh/h, ks/x, iy/y...).
* **Screen** a customer name against every listed name and alias: candidates
  come from blocking keys, so a customer is compared with hundreds of names, not
  thousands; each candidate is scored by aligning tokens with Jaro-Winkler, and
  a date of birth that disagrees discounts the score.
* **Evaluate on the real list, honestly.** Listed parties are split in half by
  a hash of their schedule and item number. On the development half, customer
  names are generated as the variants that reach a screening desk (reordered,
  typo, transliteration, dropped patronymic, an alias, an initial) and the
  alert threshold is chosen by a rule fixed in advance: the highest threshold
  that still catches 95% of the standard variants. That threshold is then
  reported, unchanged, on the held-out half, with the alert rate on synthetic
  Canadian customers and on "hard negatives" who share a listed surname.

It is a screening exercise, not a compliance product: one list (not the UN,
Criminal Code or other lists a reporting entity must screen), names only (no
ongoing monitoring, no beneficial ownership, no PEP screening), and no decision
on whether a match is true. That decision belongs to a person.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as cfg  # noqa: E402

SOURCE_URL = (
    "https://www.international.gc.ca/world-monde/assets/office_docs/"
    "international_relations-relations_internationales/sanctions/sema-lmes.xml"
)
SOURCE_PAGE = (
    "https://www.international.gc.ca/world-monde/international_relations-"
    "relations_internationales/sanctions/consolidated-consolide.aspx?lang=eng"
)
RAW_PATH = ROOT / "data" / "raw" / "sema-lmes.xml"
RESULT_PATH = cfg.RESULTS_DIR / "sanctions_screening.json"
README = ROOT / "README.md"
BEGIN, END = "<!-- sanctions-screening:begin -->", "<!-- sanctions-screening:end -->"

SEED = 20261023
TARGET_RECALL = 0.95  # the rule that picks the threshold, fixed before any run
NEGATIVES_PER_SPLIT = 4000
HARD_NEGATIVES_PER_SPLIT = 1000
MAX_PARTIES_PER_VARIANT = 600

# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

# BGN/PCGN-style romanisation, extended with the Ukrainian and Belarusian
# letters the list uses. One table for every alias in Cyrillic.
CYRILLIC = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "ґ": "g",
    "д": "d",
    "е": "e",
    "ё": "yo",
    "є": "ye",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "і": "i",
    "ї": "yi",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ў": "w",
    "ф": "f",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}

# Legal forms and filler that say nothing about who a party is. Matching on them
# would make every "LLC" a candidate for every other.
STOP_TOKENS = frozenset(
    {
        "llc",
        "ltd",
        "limited",
        "jsc",
        "ojsc",
        "pjsc",
        "cjsc",
        "ooo",
        "oao",
        "zao",
        "ao",
        "pao",
        "inc",
        "corp",
        "corporation",
        "co",
        "company",
        "group",
        "gmbh",
        "sa",
        "plc",
        "llp",
        "the",
        "of",
        "and",
        "de",
        "la",
        "el",
        "al",
        "fzco",
        "fze",
        "jv",
        "ltda",
        "srl",
        # Connectives the list writes inside an alias ("Katsiaryna or Katerina").
        "or",
        "also",
        "known",
        "as",
    }
)


def transliterate(text: str) -> str:
    out = []
    for char in text:
        lower = char.lower()
        out.append(CYRILLIC.get(lower, char))
    return "".join(out)


def normalize(text: str) -> str:
    """Lowercase ASCII words: transliterated, unaccented, punctuation removed."""
    text = transliterate(text or "")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("&", " and ").replace("'", "")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


FOLDS = [
    ("shch", "sh"),
    ("sch", "sh"),
    ("tch", "ch"),
    ("dzh", "j"),
    ("zh", "j"),
    ("kh", "h"),
    ("ph", "f"),
    ("th", "t"),
    ("ck", "k"),
    ("ks", "x"),
    ("cz", "ch"),
    ("iy", "i"),
    ("yy", "i"),
    ("ii", "i"),
    ("yi", "i"),
    ("ij", "i"),
    ("ye", "e"),
    ("ie", "e"),
    ("yo", "o"),
    ("jo", "o"),
    ("yu", "u"),
    ("ju", "u"),
    ("ya", "a"),
    ("ja", "a"),
    ("w", "v"),
    ("q", "k"),
    ("y", "i"),
]


def fold(token: str) -> str:
    """One key for the spellings romanisation varies: Aleksandr/Alexandr,
    Yevgeniy/Evgeny, Khodorkovsky/Hodorkovskiy."""
    for a, b in FOLDS:
        token = token.replace(a, b)
    token = re.sub(r"c(?!h)", "k", token)
    return re.sub(r"(.)\1+", r"\1", token)


def tokens(name: str) -> tuple[str, ...]:
    return tuple(t for t in normalize(name).split() if t not in STOP_TOKENS)


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Party:
    key: str  # schedule|item: stable across list versions
    kind: str  # individual | entity | ship
    names: tuple[str, ...]  # primary name first, then aliases
    birth_year: int | None
    country: str
    listed_on: str


def _fields(record: ET.Element) -> dict[str, str]:
    """English half of each bilingual tag ('LastName-NomDeFamille' -> 'LastName')."""
    return {child.tag.split("-")[0]: (child.text or "").strip() for child in record}


def split_aliases(text: str, person: bool = False) -> list[str]:
    """One alias per name. For people the list also joins aliases with commas
    ("Galina ... LUKASHENKA, Halina ... LUKASHENKO"); an entity's comma can be part
    of its name, so entities are not split there."""
    parts = re.split(r";|\n|\s/\s|\baka\b|\ba\.k\.a\.", text or "")
    if person:
        parts = [q for part in parts for q in part.split(",")]
    return [q.strip(" ,.") for q in parts if q.strip(" ,.")]


def script_of(name: str) -> str:
    """latin, cyrillic or other. Only the first two are screened: an alias in
    Persian, Chinese, Korean or Burmese script normalises to nothing here, and
    is counted as not screened rather than silently matching nothing."""
    scripts = set()
    for char in name:
        if char.isalpha():
            label = unicodedata.name(char, "")
            scripts.add(
                "latin"
                if label.startswith("LATIN")
                else "cyrillic"
                if label.startswith("CYRILLIC")
                else "other"
            )
    if "other" in scripts or not scripts:
        return "other"
    return "cyrillic" if "cyrillic" in scripts else "latin"


def parse_list(xml: bytes) -> list[Party]:
    root = ET.fromstring(xml)
    parties = []
    for record in root.iter("record"):
        f = _fields(record)
        last, given = f.get("LastName", ""), f.get("GivenName", "")
        entity = next(
            (v for k, v in f.items() if k.startswith(("Entity", "EntityOrShip")) and v), ""
        )
        ship = bool(f.get("ShipIMONumber") or f.get("ShipImoNumber"))
        if last or given:
            kind, primary = "individual", " ".join(p for p in (given, last) if p)
        elif entity:
            kind, primary = ("ship" if ship else "entity"), entity
        else:
            continue
        aliases = split_aliases(f.get("Aliases", ""), person=bool(last or given))
        year = re.search(r"\b(1[89]\d\d|20\d\d)\b", f.get("DateOfBirthOrShipBuildDate", ""))
        parties.append(
            Party(
                key=f"{f.get('Schedule', '')}|{f.get('Item', '')}|{f.get('Country', '')}",
                kind=kind,
                names=tuple(dict.fromkeys([primary, *aliases])),
                birth_year=int(year.group(1)) if year and kind == "individual" else None,
                country=f.get("Country", ""),
                listed_on=f.get("DateOfListing", ""),
            )
        )
    return parties


def describe(xml: bytes, parties: list[Party]) -> dict:
    """Aggregates only: what the run saw, never who."""
    root = ET.fromstring(xml)
    tags = Counter(child.tag.split("-")[0] for record in root.iter("record") for child in record)
    scripts = Counter(script_of(n) for p in parties for n in p.names[1:])
    return {
        "records": sum(1 for _ in root.iter("record")),
        "parties": Counter(p.kind for p in parties),
        "names_including_aliases": sum(len(p.names) for p in parties),
        "names_not_screened_other_script": sum(
            script_of(n) == "other" for p in parties for n in p.names
        ),
        "primary_names_by_script": dict(Counter(script_of(p.names[0]) for p in parties)),
        "aliases_by_script": dict(scripts),
        "with_birth_year": sum(p.birth_year is not None for p in parties),
        "countries_or_regimes": len({p.country for p in parties}),
        "fields_seen": dict(tags),
        "sha256": hashlib.sha256(xml).hexdigest(),
        "bytes": len(xml),
    }


# ---------------------------------------------------------------------------
# Screening
# ---------------------------------------------------------------------------


def _keys(folded: tuple[str, ...]) -> set[str]:
    keys = set()
    for t in folded:
        if len(t) < 3:
            # "Li Na", "Ri Il": a name of short tokens still needs a way in.
            keys.add("w:" + t)
            continue
        consonants = re.sub(r"[aeiou]", "", t)
        keys |= {"p:" + t[:3], "s:" + t[-3:]}
        if len(consonants) >= 3:
            keys.add("c:" + consonants[:3])
    return keys


@dataclass
class Index:
    parties: list[Party]
    names: list[tuple[int, str, tuple[str, ...]]] = field(
        default_factory=list
    )  # party, display, folded
    blocks: dict[str, set[int]] = field(default_factory=lambda: defaultdict(set))

    @classmethod
    def build(cls, parties: list[Party]) -> Index:
        index = cls(parties)
        for p_i, party in enumerate(parties):
            for name in party.names:
                if script_of(name) == "other":
                    continue
                folded = tuple(fold(t) for t in tokens(name))
                if not folded:
                    continue
                n_i = len(index.names)
                index.names.append((p_i, name, folded))
                for key in _keys(folded):
                    index.blocks[key].add(n_i)
        return index

    def candidates(self, folded: tuple[str, ...]) -> set[int]:
        out: set[int] = set()
        for key in _keys(folded):
            out |= self.blocks.get(key, set())
        return out


def similarity(query: tuple[str, ...], listed: tuple[str, ...]) -> float:
    """Align tokens one to one, best pairs first; 0..1.

    The score is mostly how well each query token is explained (a customer who
    omits a patronymic is still the same person), and partly how much of the
    listed name is covered (one shared surname is not a match)."""
    from rapidfuzz.distance import JaroWinkler

    if not query or not listed:
        return 0.0
    pairs = []
    for i, q in enumerate(query):
        for j, n in enumerate(listed):
            if len(q) == 1 or len(n) == 1:  # an initial matches its word
                sim = 0.9 if q[0] == n[0] else 0.0
            else:
                sim = JaroWinkler.similarity(q, n)
            pairs.append((sim, i, j))
    used_q, used_n, total = set(), set(), 0.0
    for sim, i, j in sorted(pairs, reverse=True):
        if i in used_q or j in used_n:
            continue
        used_q.add(i)
        used_n.add(j)
        total += sim
    explained = total / len(query)
    covered = len(used_n) / len(listed)
    return 0.85 * explained + 0.15 * covered


def screen(index: Index, name: str, birth_year: int | None = None, top: int = 3) -> list[dict]:
    folded = tuple(fold(t) for t in tokens(name))
    best: dict[int, tuple[float, str]] = {}
    for n_i in index.candidates(folded):
        p_i, display, listed = index.names[n_i]
        score = similarity(folded, listed)
        party = index.parties[p_i]
        if birth_year and party.birth_year:
            score = (
                min(1.0, score + 0.02) if abs(birth_year - party.birth_year) <= 1 else score * 0.85
            )
        if score > best.get(p_i, (0.0, ""))[0]:
            best[p_i] = (score, display)
    ranked = sorted(best.items(), key=lambda kv: kv[1][0], reverse=True)[:top]
    return [
        {
            "party": index.parties[p].key,
            "kind": index.parties[p].kind,
            "score": round(s, 4),
            "matched_name": shown,
        }
        for p, (s, shown) in ranked
    ]


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def split_of(party: Party) -> str:
    """dev or test, by a hash of the party's key, so the split survives list updates."""
    return "dev" if hashlib.sha256(party.key.encode()).digest()[0] % 2 == 0 else "test"


ROMANISATION_SWAPS = [
    ("ks", "x"),
    ("x", "ks"),
    ("iy", "y"),
    ("y", "i"),
    ("kh", "h"),
    ("ie", "ye"),
    ("zh", "j"),
    ("sh", "sch"),
    ("ts", "tz"),
    ("v", "w"),
    ("yu", "iu"),
    ("ya", "ia"),
]


def _typo(word: str, rng: random.Random) -> str:
    if len(word) < 5:
        return word
    i = rng.randrange(1, len(word) - 1)
    op = rng.choice(["swap", "drop", "sub"])
    if op == "swap":
        return word[:i] + word[i + 1] + word[i] + word[i + 2 :]
    if op == "drop":
        return word[:i] + word[i + 1 :]
    return word[:i] + rng.choice("aeiourstnlk") + word[i + 1 :]


def variants(party: Party, rng: random.Random) -> dict[str, str]:
    """The forms a listed name takes when a customer types it. Only types that
    apply to this party are returned."""
    primary = party.names[0]
    words = primary.split()
    out: dict[str, str] = {"exact": primary}
    if len(words) >= 2:
        out["reordered"] = f"{words[-1]}, {' '.join(words[:-1])}"
    plain = unicodedata.normalize("NFKD", primary).encode("ascii", "ignore").decode().upper()
    if plain.strip():
        out["case_and_accents"] = plain
    long = [i for i, w in enumerate(words) if len(w) >= 5]
    if long:
        w = words[:]
        i = rng.choice(long)
        w[i] = _typo(w[i], rng)
        out["one_typo"] = " ".join(w)
        if len(long) >= 2:
            j = rng.choice([k for k in long if k != i])
            w[j] = _typo(w[j], rng)
            out["two_typos"] = " ".join(w)
    lower = primary.lower()
    swaps = [(a, b) for a, b in ROMANISATION_SWAPS if a in lower]
    if swaps:
        a, b = rng.choice(swaps)
        out["transliteration"] = lower.replace(a, b, 1).title()
    if party.kind == "individual" and len(words) >= 3:
        out["dropped_middle_name"] = f"{words[0]} {words[-1]}"
    if party.kind == "individual" and len(words) >= 2:
        out["initial_for_given_name"] = f"{words[0][0]}. {' '.join(words[1:])}"
    latin_aliases = [a for a in party.names[1:] if script_of(a) == "latin"]
    cyrillic_aliases = [a for a in party.names[1:] if script_of(a) == "cyrillic"]
    if latin_aliases:
        out["latin_alias"] = rng.choice(latin_aliases)
    if cyrillic_aliases:
        out["cyrillic_alias"] = rng.choice(cyrillic_aliases)
    return out


STANDARD = (
    "exact",
    "reordered",
    "case_and_accents",
    "one_typo",
    "transliteration",
    "dropped_middle_name",
    "latin_alias",
    "cyrillic_alias",
)
STRESS = ("two_typos", "initial_for_given_name")


def negatives(n: int, seed: int) -> list[str]:
    """Synthetic customers: mostly Canadian names, with Slavic names romanised the
    way a Canadian bank's onboarding form would record them."""
    from faker import Faker

    fakers = {"en_CA": 0.45, "fr_CA": 0.25, "ru_RU": 0.15, "uk_UA": 0.15}
    rng = random.Random(seed)
    out = []
    pools = {loc: Faker(loc) for loc in fakers}
    for f in pools.values():
        f.seed_instance(seed)
    for _ in range(n):
        loc = rng.choices(list(fakers), weights=list(fakers.values()))[0]
        f = pools[loc]
        name = f"{f.first_name()} {f.last_name()}"
        out.append(transliterate(name).title() if loc in ("ru_RU", "uk_UA") else name)
    return out


def hard_negatives(parties: list[Party], n: int, seed: int) -> list[tuple[str, int | None]]:
    """A listed surname with someone else's given name and a different birth year:
    the namesakes a screening desk clears every day."""
    from faker import Faker

    rng = random.Random(seed)
    fakers = [Faker("ru_RU"), Faker("uk_UA"), Faker("en_CA")]
    for f in fakers:
        f.seed_instance(seed)
    people = [p for p in parties if p.kind == "individual" and len(p.names[0].split()) >= 2]
    listed = {" ".join(sorted(tokens(name))) for p in parties for name in p.names}
    out = []
    for _ in range(min(n, len(people) * 3)):
        p = rng.choice(people)
        given = transliterate(rng.choice(fakers).first_name()).title()
        candidate = f"{given} {p.names[0].split()[-1]}"
        # Someone else's given name, never a name that is itself on the list.
        if (
            given.lower() == p.names[0].split()[0].lower()
            or " ".join(sorted(tokens(candidate))) in listed
        ):
            continue
        year = (p.birth_year + rng.choice([-30, -20, 15, 25])) if p.birth_year else None
        out.append((candidate, year))
    return out[:n]


def _score_all(index: Index, queries: list[tuple[str, int | None]]) -> list[float]:
    return [(screen(index, q, year, top=1) or [{"score": 0.0}])[0]["score"] for q, year in queries]


def evaluate(parties: list[Party], seed: int = SEED) -> dict:
    index = Index.build(parties)
    rng = random.Random(seed)
    by_split: dict[str, dict[str, list[tuple[str, int | None]]]] = {
        "dev": defaultdict(list),
        "test": defaultdict(list),
    }
    for party in parties:
        for vtype, text in variants(party, rng).items():
            by_split[split_of(party)][vtype].append((text, party.birth_year))
    for split in by_split:
        for items in by_split[split].values():
            rng.shuffle(items)
            del items[MAX_PARTIES_PER_VARIANT:]

    scores = {s: {v: _score_all(index, q) for v, q in by_split[s].items()} for s in by_split}
    neg = {
        "dev": _score_all(index, [(n, None) for n in negatives(NEGATIVES_PER_SPLIT, seed)]),
        "test": _score_all(index, [(n, None) for n in negatives(NEGATIVES_PER_SPLIT, seed + 1)]),
    }
    hard = {
        "dev": _score_all(index, hard_negatives(parties, HARD_NEGATIVES_PER_SPLIT, seed)),
        "test": _score_all(index, hard_negatives(parties, HARD_NEGATIVES_PER_SPLIT, seed + 1)),
    }

    def recall(values: list[float], t: float) -> float:
        return sum(v >= t for v in values) / len(values) if values else float("nan")

    # The rule, fixed before the first run: the highest threshold that still
    # catches TARGET_RECALL of the standard variants on the development half.
    dev_standard = [x for v in STANDARD for x in scores["dev"].get(v, [])]
    grid = [round(0.70 + 0.005 * k, 3) for k in range(61)]
    threshold = max((t for t in grid if recall(dev_standard, t) >= TARGET_RECALL), default=grid[0])

    def report(split: str) -> dict:
        standard = [x for v in STANDARD for x in scores[split].get(v, [])]
        return {
            "variants": {
                v: {"names": len(s), "recall": recall(s, threshold)}
                for v, s in sorted(scores[split].items())
            },
            "standard_recall": recall(standard, threshold),
            "customers": len(neg[split]),
            "customer_alert_rate": recall(neg[split], threshold),
            "alerts_per_10k_customers": round(10_000 * recall(neg[split], threshold), 1),
            "hard_negatives": len(hard[split]),
            "hard_negative_alert_rate": recall(hard[split], threshold),
        }

    curve = [
        {
            "threshold": t,
            "dev_standard_recall": recall(dev_standard, t),
            "dev_alerts_per_10k_customers": round(10_000 * recall(neg["dev"], t), 1),
        }
        for t in grid
        if 0.80 <= t <= 0.97 and round(t * 200) % 2 == 0
    ]
    return {
        "rule": {
            "target_standard_recall_on_dev": TARGET_RECALL,
            "threshold_chosen_on": "dev",
            "reported_on": "test (held-out half of the list)",
            "seed": seed,
            "standard_variants": list(STANDARD),
            "stress_variants": list(STRESS),
        },
        "threshold": threshold,
        "dev": report("dev"),
        "test": report("test"),
        "curve_on_dev": curve,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

LABELS = {
    "exact": "Exact",
    "reordered": "Surname first",
    "case_and_accents": "Upper case, no accents",
    "one_typo": "One typo",
    "two_typos": "Two typos",
    "transliteration": "Another romanisation",
    "dropped_middle_name": "Patronymic dropped",
    "initial_for_given_name": "Initial for given name",
    "latin_alias": "A listed alias (Latin)",
    "cyrillic_alias": "A listed alias (Cyrillic)",
}


def readme_block(result: dict) -> str:
    if not result.get("evaluation"):
        return f"{BEGIN}\n_The list has not been screened yet._\n{END}"
    ev, meta = result["evaluation"], result["list"]
    test = ev["test"]

    def pct(x: float) -> str:
        return f"{100 * x:.1f}%"

    plural = {"individual": "individuals", "entity": "entities", "ship": "ships"}
    kinds = ", ".join(
        f"{v:,} {k if v == 1 else plural.get(k, k + 's')}"
        for k, v in sorted(meta["parties"].items())
    )
    names = meta["names_including_aliases"]
    unscreened = meta.get("names_not_screened_other_script", 0)
    primaries = meta.get("primary_names_by_script", {})
    latin_primary = primaries.get("latin", 0)
    primary_line = (
        f"{latin_primary:,} of {sum(primaries.values()):,} listed parties have a Latin-script "
        f"primary name, which is screened."
        if primaries
        else ""
    )
    target = pct(ev["rule"]["target_standard_recall_on_dev"])
    lines = [
        BEGIN,
        f"List retrieved {result['retrieved_at'][:10]} ({kinds}; {names:,} names including "
        f"aliases; SHA-256 `{meta['sha256'][:12]}`). Threshold **{ev['threshold']:.3f}**, "
        f"chosen on the development half by the fixed rule (catch {target} of standard "
        f"variants) and reported unchanged on the held-out half:",
        "",
        "| Customer name typed as | Names (held out) | Alerted |",
        "|---|---:|---:|",
    ]
    order = list(STANDARD) + list(STRESS)
    for v in order:
        if v in test["variants"]:
            row = test["variants"][v]
            tag = " *(stress)*" if v in STRESS else ""
            lines.append(f"| {LABELS[v]}{tag} | {row['names']:,} | {pct(row['recall'])} |")
    lines += [
        f"| **All standard variants** | | **{pct(test['standard_recall'])}** |",
        "",
        f"On {test['customers']:,} synthetic Canadian customers the same threshold alerts on "
        f"{pct(test['customer_alert_rate'])} "
        f"(**{test['alerts_per_10k_customers']:,.1f} per 10,000**); on "
        f"{test['hard_negatives']:,} namesakes who share a listed surname but not the given "
        f"name or birth year, {pct(test['hard_negative_alert_rate'])}. Every alert is a "
        f"review, not a finding.",
        "",
        f"Not screened: {unscreened:,} aliases written in a script other than Latin or "
        f"Cyrillic (Persian, Chinese, Korean and others), which this matcher cannot "
        f"romanise." + (f" {primary_line}" if primary_line else ""),
        END,
    ]
    return "\n".join(lines)


def write_readme(result: dict, path: Path = README) -> bool:
    text = path.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        return False
    head, rest = text.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    path.write_text(head + readme_block(result) + tail, encoding="utf-8")
    return True


def download(path: Path = RAW_PATH) -> bytes:
    import urllib.request

    request = urllib.request.Request(
        SOURCE_URL, headers={"User-Agent": "aml-transaction-monitoring/1.0"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--download", action="store_true", help="fetch the current list first")
    parser.add_argument("--xml", type=Path, default=RAW_PATH)
    parser.add_argument("--evaluate", action="store_true")
    parser.add_argument("--write-readme", action="store_true")
    args = parser.parse_args(argv)

    xml = download(args.xml) if args.download else args.xml.read_bytes()
    parties = parse_list(xml)
    meta = describe(xml, parties)
    print(json.dumps({k: v for k, v in meta.items() if k != "sha256"}, indent=1, default=dict))
    result = {
        "source": SOURCE_URL,
        "source_page": SOURCE_PAGE,
        "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "list": meta,
    }
    if args.evaluate:
        result["evaluation"] = evaluate(parties)
        print(json.dumps({k: result["evaluation"][k] for k in ("threshold", "test")}, indent=1))
        RESULT_PATH.write_text(json.dumps(result, indent=2, default=dict) + "\n", encoding="utf-8")
    if args.write_readme:
        write_readme(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
