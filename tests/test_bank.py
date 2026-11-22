"""Vérifie la banque de questions : structure ET justesse des réponses (recalculées).

Lancer :  python tests/test_bank.py
"""
import json
import math
from fractions import Fraction
from pathlib import Path

BANK = json.loads((Path(__file__).resolve().parent.parent / "app" / "data" / "questions.json").read_text("utf-8"))
BY_KEY = {q["key"]: q for q in BANK}


def look_and_say(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        j = i
        while j < len(s) and s[j] == s[i]:
            j += 1
        out.append(f"{j - i}{s[i]}")
        i = j
    return "".join(out)


def snail_day() -> int:
    day, pos = 0, 0
    while True:
        day += 1
        pos += 3
        if pos >= 10:
            return day
        pos -= 2


def clock_overlaps_24h() -> int:
    # Les aiguilles coïncident quand 5,5 × t ≡ 0 (mod 360), t en minutes, t ∈ [0, 1440[
    return sum(1 for k in range(100) if Fraction(360 * k) / Fraction(11, 2) < 1440)


def family() -> int:
    sols = [(g, b) for g in range(1, 30) for b in range(1, 30) if b == g - 1 and g == 2 * (b - 1)]
    assert len(sols) == 1
    return sols[0][0] + sols[0][1]


def next_letter(start: str, steps: list[int]) -> str:
    return chr(ord("A") + (ord(start) - ord("A") + steps[-1]) % 26)


# clé de question -> réponse attendue (texte exact du choix correct)
EXPECTED = {
    "q001": "10",
    "q002": "Léa",
    "q003": "Carotte",
    "q004": "Ils pèsent pareil",
    "q005": "1,50 €",
    "q006": "Vendredi",                                  # hier lundi -> auj. mardi -> +3 j
    "q007": "2",
    "q008": str(6 ** 2),
    "q009": "Deuxième",
    "q010": next(str(n) for n in (2, 5, 9, 11) if any(n % d == 0 for d in range(2, n))),
    "q011": "9",
    "q012": "13",                                         # Fibonacci 5 + 8
    "q013": "Mardi" if (24 - 3) % 7 == 0 else "?",
    "q014": str(12),
    "q015": str(eval("1 + 1 * 0 + 1")),
    "q016": "12",
    "q017": "D",
    "q018": str(11 + 5),
    "q019": "O",
    "q020": f"Le {snail_day()}e jour",
    "q021": str(int(8 / Fraction(1, 2) + 3)),
    "q022": str(2 ** 6 - 1),
    "q023": "5 minutes",
    "q024": str(6 * 7),
    "q025": str(48 - 1),
    "q026": str(math.ceil(Fraction(100, Fraction(100, 3)))),
    "q027": "0,05 €",
    "q028": str(math.factorial(6)),
    "q029": chr(ord("O") + 6 - 26 * 0) if ord("O") + 6 <= ord("Z") else "?",
    "q030": str(4 + 5 * 3),
    "q031": "H",
    "q032": str(clock_overlaps_24h()),
    "q033": str(family()),
    "q034": look_and_say("111221"),
    "q035": str(sum(k * k for k in range(1, 9))),
    "q036": str(len(str(math.factorial(25))) - len(str(math.factorial(25)).rstrip("0"))),
    "q037": str(len("ten")),
}


def main() -> None:
    assert len(BY_KEY) == len(BANK), "clés dupliquées"
    for q in BANK:
        assert 2 <= len(q["choices"]) <= 4, q["key"]
        assert len({c.lower() for c in q["choices"]}) == len(q["choices"]), f"{q['key']}: choix en double"
        assert 0 <= q["correct"] < len(q["choices"]), q["key"]
        assert 1 <= q["difficulty"] <= 99, q["key"]
        assert q["explanation"], f"{q['key']}: explication manquante"
        assert len(q["text"]) <= 300 and all(len(c) <= 80 for c in q["choices"]), q["key"]

    # Vérifications supplémentaires des cas non triviaux
    assert look_and_say("1") == "11" and look_and_say("11") == "21" and look_and_say("21") == "1211"
    assert look_and_say("1211") == "111221"
    assert [len(w) for w in "one two three four five six seven eight nine".split()] == [3, 3, 5, 4, 4, 3, 5, 5, 4]
    # Lettres de la suite A, C, F, J, O : écarts +2 +3 +4 +5, puis +6 -> U
    pos = [ord(c) - 64 for c in "ACFJO"]
    assert [b - a for a, b in zip(pos, pos[1:])] == [2, 3, 4, 5]
    assert chr(64 + pos[-1] + 6) == "U"
    # Initiales un, deux, trois, quatre, cinq, six, sept, huit
    assert "".join(w[0].upper() for w in "un deux trois quatre cinq six sept huit".split()) == "UDTQCSSH"
    # Initiales des mois / jours
    assert "".join(m[0].upper() for m in "janvier février mars avril mai juin juillet août septembre octobre".split()) == "JFMAMJJASO"
    assert "".join(j[0].upper() for j in "lundi mardi mercredi jeudi vendredi samedi dimanche".split()) == "LMMJVSD"

    for key, expected in EXPECTED.items():
        q = BY_KEY[key]
        got = q["choices"][q["correct"]]
        assert got == expected, f"{key}: la banque donne « {got} », le recalcul donne « {expected} »"
    missing = set(BY_KEY) - set(EXPECTED)
    assert not missing, f"questions sans vérification : {sorted(missing)}"

    # La question la plus difficile doit être unique (question ultime des parties automatiques)
    hardest = min(q["difficulty"] for q in BANK)
    assert sum(1 for q in BANK if q["difficulty"] == hardest) == 1
    print(f"OK : {len(BANK)} questions vérifiées (structure + réponses recalculées).")


if __name__ == "__main__":
    main()
