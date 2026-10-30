"""Post-contest similarity check: who handed in (nearly) the same code.

Sources are reduced to tokens that survive the usual disguises: identifiers
become ``ID``, numbers ``N``, strings and characters ``S``; comments,
whitespace and preprocessor lines (``#include``, ``#define``) disappear. Then
the classic winnowing fingerprint (MOSS's technique): hash every k tokens in a
row, keep the smallest hash in each window, compare the sets.

Code most of the room shares — a fast-I/O template, a typedef block — would
make everyone look alike, so fingerprints found in more than half of a
problem's *distinct* solutions are ignored (identical copies count once, so
a solution most of the room copied is still caught).

A high score is a reason to look, not a verdict: short problems have few ways
to be solved, and the page shows the two sources side by side.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass
from itertools import combinations

K = 8           # tokens per fingerprint
WINDOW = 4      # winnowing window
COMMON = 0.5    # fingerprints in more than this share of solutions are boilerplate
MIN_FINGERPRINTS = 8  # below this a solution is too small to compare meaningfully

_CPP = set("""
    auto bool break case catch char class const constexpr continue default delete do double else enum
    explicit extern false float for friend goto if inline int long namespace new nullptr operator private
    protected public register return short signed sizeof static struct switch template this throw true
    try typedef typename union unsigned using virtual void volatile while
    std vector map set pair string queue stack deque priority_queue unordered_map unordered_set array
    cin cout cerr endl printf scanf sort min max swap
""".split())
_JAVA = set("""
    abstract boolean break byte case catch char class continue default do double else extends final
    finally float for if implements import instanceof int interface long new null package private
    protected public return short static super switch this throw throws try void while true false
    String System Scanner Math Arrays ArrayList HashMap List Map out in println print nextInt nextLong
""".split())
_PYTHON = set("""
    and as assert break class continue def del elif else except False finally for from global if
    import in is lambda None nonlocal not or pass raise return True try while with yield
    print input range len int str list dict tuple set map sorted sum min max abs enumerate zip
""".split())
# Words that keep their name: what a language gives you, not what the author chose.
_KEYWORDS = {"cpp": _CPP, "java21": _JAVA, "py12": _PYTHON}

_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<comment>//[^\n]*|/\*.*?\*/|\#[^\n]*)
  | (?P<str>\"\"\".*?\"\"\"|'''.*?'''|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')
  | (?P<num>\d[\w.]*)
  | (?P<id>[A-Za-z_]\w*)
  | (?P<op><<=|>>=|\+\+|--|->|::|<=|>=|==|!=|&&|\|\||<<|>>|[-+*/%=<>!&|^~?:;,.(){}\[\]])
""", re.S | re.X)


def tokens(source: str, language: str = "cpp") -> list[str]:
    keep = _KEYWORDS.get(language, _CPP)
    out = []
    for m in _TOKEN.finditer(source):
        kind = m.lastgroup
        if kind in ("ws",):
            continue
        if kind == "comment":
            # In Python '#' starts a comment; in C/C++ it starts a preprocessor
            # line. Both are dropped.
            continue
        text = m.group()
        if kind == "str":
            out.append("S")
        elif kind == "num":
            out.append("N")
        elif kind == "id":
            out.append(text if text in keep else "ID")
        else:
            out.append(text)
    return out


def fingerprints(toks: list[str]) -> set[int]:
    if len(toks) < K:
        return {zlib.crc32(" ".join(toks).encode())} if toks else set()
    hashes = [zlib.crc32(" ".join(toks[i:i + K]).encode()) for i in range(len(toks) - K + 1)]
    if len(hashes) <= WINDOW:
        return {min(hashes)}
    return {min(hashes[i:i + WINDOW]) for i in range(len(hashes) - WINDOW + 1)}


@dataclass
class Pair:
    a: int            # submission ids
    b: int
    score: float      # shared / smaller set: 1.0 = one is contained in the other
    shared: int


def compare(solutions: dict[int, str], languages: dict[int, str] | None = None,
            threshold: float = 0.6) -> list[Pair]:
    """``solutions``: submission id -> source. Pairs at or above ``threshold``, best first."""
    toks = {sid: tokens(src, (languages or {}).get(sid, "cpp")) for sid, src in solutions.items()}
    prints = {sid: fingerprints(t) for sid, t in toks.items()}
    # How common is a fingerprint? Counted over *distinct* solutions: copies of
    # one solution count once, or a solution most of the room copied would
    # look like boilerplate and hide itself.
    distinct = {tuple(t): prints[sid] for sid, t in toks.items()}
    counts: dict[int, int] = {}
    for fp in distinct.values():
        for h in fp:
            counts[h] = counts.get(h, 0) + 1
    if len(distinct) >= 4:  # with fewer, "most of the room" means nothing
        common = {h for h, n in counts.items() if n > COMMON * len(distinct)}
        prints = {sid: fp - common for sid, fp in prints.items()}
    langs = languages or {}
    pairs = []
    for (a, fa), (b, fb) in combinations(sorted(prints.items()), 2):
        if langs.get(a, "cpp") != langs.get(b, "cpp"):
            continue  # a translation is not what this looks for
        if min(len(fa), len(fb)) < MIN_FINGERPRINTS:
            continue
        shared = len(fa & fb)
        score = shared / min(len(fa), len(fb))
        if score >= threshold:
            pairs.append(Pair(a, b, round(score, 3), shared))
    return sorted(pairs, key=lambda p: -p.score)


def clusters(pairs: list[Pair]) -> list[dict]:
    """Group pairs into sets of solutions linked by similarity, biggest first.

    Ten people with one solution are one finding, not 45 pairs.
    """
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for p in pairs:
        parent[find(p.a)] = find(p.b)
    groups: dict[int, dict] = {}
    for p in pairs:
        g = groups.setdefault(find(p.a), {"members": set(), "pairs": []})
        g["members"] |= {p.a, p.b}
        g["pairs"].append(p)
    out = [{"members": sorted(g["members"]), "best": max(x.score for x in g["pairs"]),
            "pairs": sorted(g["pairs"], key=lambda x: -x.score)} for g in groups.values()]
    return sorted(out, key=lambda g: (-len(g["members"]), -g["best"]))
