#!/usr/bin/env python3
"""定跡のラインごとの勝率の表（arena の 1行=1対局の記録から）。

    python3 scripts/ci/open_lines_summary.py <all-games.jsonl> <rl_openings.json>
    python3 scripts/ci/open_lines_summary.py --self-test

`open:book:<戦略名>` / `open:<ライン>:<戦略名>`（src/rl/forced.rs）で包んだ側は、局ごとに定跡のラインを1本指す。
arena はそのラインの添字を記録の `open_line_a`（候補）/ `open_line_b`（基準）に残すので、ここでライン × 基準の表にする。
勝率はいつも**候補（A）から見た値**で、引き分けを除く（arena の総合の表と同じ数え方）。包んだ側が無い記録なら何も出さない。

序盤を強制して学習したネットは初期局面から自分で指したことが無いので、候補の側を `open:book:` で包んで測る。
系統ごとに同じ確率で引く（学習の `--open-weight category` と同じ）ので、1ラインあたりの局数は全体の 2.2〜2.8%。
"""

import collections
import json
import sys


def tally(rows, key):
    """{(ラインの添字, 基準): [勝, 負, 分]}（`key` が null の行は数えない）"""
    out = collections.defaultdict(lambda: [0, 0, 0])
    for r in rows:
        line = r.get(key)
        if line is None:
            continue
        s = r["score_a"]
        out[(line, r["baseline"])][0 if s > 0.5 else 1 if s < 0.5 else 2] += 1
    return out


def cell(wld):
    w, l, d = wld
    if w + l + d == 0:
        return "–"
    rate = f"{100 * w / (w + l):.0f}%" if w + l else "–"
    return f"{rate}（{w}-{l}-{d}）"


def add(a, b):
    return [x + y for x, y in zip(a, b)]


def table(rows, key, book, title):
    t = tally(rows, key)
    if not t:
        return []
    baselines = list(dict.fromkeys(r["baseline"] for r in rows))
    lines = sorted({k[0] for k in t})
    out = [f"### {title}", "", "候補（A）から見た勝率（引き分けを除く）と 勝-負-分。", "",
           "| # | ライン | 系統 | " + " | ".join(baselines) + " | 計 |", "|---|---|---|" + "---|" * (len(baselines) + 1)]
    by_cat = collections.defaultdict(lambda: collections.defaultdict(lambda: [0, 0, 0]))
    total = collections.defaultdict(lambda: [0, 0, 0])
    for i in lines:
        meta = book[i] if 0 <= i < len(book) else {}
        name, cat = meta.get("name", "?"), meta.get("category", "?")
        row = [t.get((i, b), [0, 0, 0]) for b in baselines]
        s = [0, 0, 0]
        for b, x in zip(baselines, row):
            s = add(s, x)
            by_cat[cat][b] = add(by_cat[cat][b], x)
            total[b] = add(total[b], x)
        out.append(f"| {i} | {name} | {cat} | " + " | ".join(cell(x) for x in row) + f" | {cell(s)} |")
    out += ["", "| 系統 | " + " | ".join(baselines) + " | 計 |", "|---|" + "---|" * (len(baselines) + 1)]
    for cat, d in list(by_cat.items()) + [("**全体**", total)]:
        s = [0, 0, 0]
        for b in baselines:
            s = add(s, d[b])
        out.append(f"| {cat} | " + " | ".join(cell(d[b]) for b in baselines) + f" | {cell(s)} |")
    return out + [""]


def render(rows, book):
    out = table(rows, "open_line_a", book, "定跡のラインごとの勝率（候補が指したライン）")
    out += table(rows, "open_line_b", book, "定跡のラインごとの勝率（基準が指したライン）")
    return ("## 定跡のライン別\n\n" + "\n".join(out)) if out else ""


def self_test():
    book = [{"name": "a", "category": "x"}, {"name": "b", "category": "x"}, {"name": "c", "category": "y"}]
    rows = [{"baseline": "p", "score_a": 1.0, "open_line_a": 0, "open_line_b": None},
            {"baseline": "p", "score_a": 0.0, "open_line_a": 0, "open_line_b": None},
            {"baseline": "p", "score_a": 0.5, "open_line_a": 2, "open_line_b": None},
            {"baseline": "q", "score_a": 1.0, "open_line_a": 2, "open_line_b": None},
            {"baseline": "q", "score_a": 1.0}]
    t = tally(rows, "open_line_a")
    assert t[(0, "p")] == [1, 1, 0] and t[(2, "p")] == [0, 0, 1] and t[(2, "q")] == [1, 0, 0] and len(t) == 3
    text = render(rows, book)
    assert "| 0 | a | x | 50%（1-1-0） | – | 50%（1-1-0） |" in text, text
    assert "| 2 | c | y | –（0-0-1） | 100%（1-0-0） | 100%（1-0-1） |" in text, text
    assert "| **全体** | 50%（1-1-1） | 100%（1-0-0） | 67%（2-1-1） |" in text, text
    assert "基準が指したライン" not in text
    assert render([{"baseline": "p", "score_a": 1.0}], book) == ""
    assert "基準が指したライン" in render([{"baseline": "p", "score_a": 1.0, "open_line_b": 1}], book)
    print("open_lines_summary self-test OK")


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        self_test()
        sys.exit(0)
    games, book_path = sys.argv[1:3]
    rows = [json.loads(x) for x in open(games, encoding="utf-8") if x.strip()]
    book = json.load(open(book_path, encoding="utf-8"))["lines"]
    text = render(rows, book)
    if text:
        print(text)
