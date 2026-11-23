#!/usr/bin/env python3
"""入力プレーンの A/B（.github/workflows/input-ab.yml）の、対局での比較と集計。

学習は tsuitate-nn の `rnad/input_ab.py`（小さい自己対局を 腕 × seed で回す）。ここは書き出された重みを arena で
総当たりにして、腕どうしの強さを比べる。標準ライブラリだけで動く。

    # 起動の引数（手動起動の入力か、リクエストファイル）を解釈して、ランナーへの配り方を決める
    python3 scripts/ci/input_ab.py plan [--request .github/ci/input-ab.request.json]
    # 学習の段ごとの artifact から、run ごとにいちばん進んだものを集める
    python3 scripts/ci/input_ab.py merge parts iab
    # 全部の run に共通して書き出されている更新から、比べる時点を選ぶ（JSON の配列を出す）
    python3 scripts/ci/input_ab.py horizons <root> --count 4
    # ある更新の重みを総当たりにする（1対 --games 局。両側とも序盤は定跡から = 学習と同じ）
    python3 scripts/ci/input_ab.py crossplay <root> --step 800 --games 50 --arena target/release/arena \\
        --out results/step800.jsonl [--refs "heuristic rl_v15"]
    # 表と判定（markdown）
    python3 scripts/ci/input_ab.py table results/ --control v4 --treat v5 [--depend depend.json]

**比べ方**: 重みは `<root>/<腕>_s<seed>/current_<更新>.bin`。同じ更新どうしを総当たりにし、腕 A の各 seed と腕 B の
各 seed の勝率を平均する。区間は seed の取り直し（両方の腕の seed を復元抽出するブートストラップ）で出す = 1局ごとの
ぶれではなく「別の seed で回し直したらどれだけ変わるか」を幅にする。腕の中の seed どうしの対局（同じ入力どうし）は、
seed だけでどれだけ強さが散るかの目安になる。
"""

import argparse
import itertools
import json
import os
import pathlib
import random
import re
import shutil
import subprocess
import sys

BOOTSTRAP = 4000
PERMUTATIONS = 20000


# 起動の引数の既定値（手動起動の入力と、リクエストファイルのキー）
DEFAULTS = {
    "arms": "v4 v5",        # 入力の版（tsuitate-nn の rnad/input_ab.py の ARMS の名前。スペース区切り）
    "extra_arms": {},       # 追加の腕 {"名前": "train_rnad.py の引数"}
    "control": "v4",        # 判定の対照
    "treat": "v5",          # 判定の候補
    "seeds": 8,             # 腕ごとの本数（seed は 1 から）
    "steps": 1600,          # 通しの更新回数の上限（1更新 64局）
    "stages": 2,            # 学習のジョブを何段つなぐか（1段 = stage_minutes 分まで。届かなければ次の段が続きを回す）
    "stage_minutes": 290,   # 1段の学習の時間（GitHub のジョブの上限 6時間の手前で止める）
    "export_every": 100,    # 重みを書き出す間隔（更新）
    "games": 100,           # 総当たりの 1対の局数
    "horizons": 4,          # 比べる時点の数（共通の書き出しから等間隔に選ぶ。最後は必ず入る）
    "refs": "heuristic rl_v15",  # 基準の相手（絶対的な強さの目安。スペース区切りの戦略名）
    "nn_ref": "main",       # tsuitate-nn の ref
}
JOBS_PER_RUNNER = 4
ARM_NAME = re.compile(r"[A-Za-z0-9]+\Z")


def cmd_plan(args):
    """GITHUB_OUTPUT に書く `キー=値` を出す。リクエストファイルが無ければ skip=true（削除の push）"""
    if args.request:
        path = pathlib.Path(args.request)
        if not path.exists():
            print("skip=true")
            return
        req = {k: v for k, v in json.loads(path.read_text()).items() if not k.startswith("_")}
    else:
        req = {k: os.environ.get("IN_" + k.upper(), "") for k in DEFAULTS}
    unknown = sorted(set(req) - set(DEFAULTS))
    if unknown:
        raise SystemExit(f"知らないキー: {unknown}（使えるのは {sorted(DEFAULTS)}。メモは _ で始める）")
    cfg = {}
    for k, d in DEFAULTS.items():
        v = req.get(k)
        if v is None or v == "":
            v = d
        if isinstance(d, int):
            v = int(v)
        elif isinstance(d, dict) and isinstance(v, str):
            v = json.loads(v)
        cfg[k] = v
    arms = cfg["arms"].split()
    extra = cfg["extra_arms"]
    if not isinstance(extra, dict) or not all(isinstance(v, str) for v in extra.values()):
        raise SystemExit('extra_arms は {"名前": "引数"} の形')
    names = arms + list(extra)
    bad = [n for n in names if not ARM_NAME.match(n) or "_s" in n]
    if bad:
        raise SystemExit(f"腕の名前は英数字だけ（{bad}）")
    if len(set(names)) != len(names) or len(names) < 2:
        raise SystemExit(f"腕は重複なしで 2つ以上（{names}）")
    for k in ("control", "treat"):
        if cfg[k] not in names:
            raise SystemExit(f"{k} の '{cfg[k]}' が腕（{names}）に無い")
    limits = {"seeds": (2, 32), "steps": (1, 100000), "stages": (1, 3), "stage_minutes": (1, 330),
              "export_every": (1, cfg["steps"]), "games": (2, 1000), "horizons": (1, 8)}
    for k, (lo, hi) in limits.items():
        if not lo <= cfg[k] <= hi:
            raise SystemExit(f"{k} は {lo}〜{hi}（{cfg[k]}）")
    if cfg["games"] % 2:
        raise SystemExit("games は偶数（先後を半分ずつ）")
    seeds = list(range(1, cfg["seeds"] + 1))
    # seed を外側に回して 1ランナー 4本ずつ = どのランナーにも腕が混ざる（ランナーの速さの差が腕に偏らない）
    queue = [f"{a}:{s}" for s in seeds for a in names]
    shards = [{"id": i // JOBS_PER_RUNNER, "only": ",".join(queue[i:i + JOBS_PER_RUNNER])}
              for i in range(0, len(queue), JOBS_PER_RUNNER)]
    if len(shards) > 20:
        raise SystemExit(f"ランナーが {len(shards)} 台になる（20 まで。seeds か腕を減らす）")
    out = {"skip": "false", "shards": json.dumps(shards), "arms": ",".join(arms),
           "extra_arms": json.dumps(extra, ensure_ascii=False), "seeds": ",".join(map(str, seeds)),
           **{k: cfg[k] for k in ("control", "treat", "steps", "stages", "stage_minutes", "export_every", "games",
                                  "horizons", "refs", "nn_ref")}}
    for k, v in out.items():
        if "\n" in str(v):
            raise SystemExit(f"{k} に改行がある")
        print(f"{k}={v}")
    print(f"腕 {names} × seed {cfg['seeds']} 本 = {len(queue)} 本を {len(shards)} ランナーで、"
          f"{cfg['steps']} 更新まで（{cfg['stages']} 段 × {cfg['stage_minutes']} 分）", file=sys.stderr)


def runs_in(root: pathlib.Path):
    """[(腕, seed, ディレクトリ)]（`<腕>_s<seed>`）"""
    out = []
    for d in sorted(root.iterdir()) if root.exists() else []:
        name, sep, seed = d.name.rpartition("_s")
        if d.is_dir() and sep and seed.isdigit():
            out.append((name, int(seed), d))
    return out


def logged_step(run: pathlib.Path) -> int:
    """log.jsonl の最後の更新の行の step（無ければ 0）"""
    path = run / "log.jsonl"
    step = 0
    if path.exists():
        for line in open(path):
            try:
                x = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "update_sec" in x and "step" in x:
                step = x["step"]
    return step


def cmd_merge(args):
    """学習の段ごとの artifact（`<src>/<artifact>/<腕>_s<seed>/`）から、run ごとにいちばん進んだものを `<dst>` へ集める。
    段をまたいで同じ run が何度も置かれる（続きを回すたび）。前の段を取り損ねて最初から回し直した段があっても、
    進んだほうを残す。同じ更新なら後の段"""
    src, dst = pathlib.Path(args.src), pathlib.Path(args.dst)
    dst.mkdir(parents=True, exist_ok=True)
    best = {}
    # artifact が1つだけのときに、取ってくる側がサブディレクトリを作らない場合にも備える（src 自体を1つの artifact と見る）
    parts = ([src] + sorted(src.iterdir())) if src.exists() else []
    for part in parts:
        if not part.is_dir():
            continue
        for _, _, d in runs_in(part):
            key = (logged_step(d), part.name)
            if d.name not in best or key > best[d.name][0]:
                best[d.name] = (key, d)
        if (part / "arms.json").exists():
            shutil.copy(part / "arms.json", dst / "arms.json")
    for name, ((step, part), d) in sorted(best.items()):
        if (dst / name).exists():
            raise SystemExit(f"{dst / name} が既にある")
        shutil.move(str(d), str(dst / name))
        print(f"{name}: {step} 更新（{part}）")
    if not best:
        print("前の段の run は無い")


def exported_steps(run: pathlib.Path):
    out = set()
    for p in run.glob("current_*.bin"):
        tail = p.stem.split("_")[1]
        if tail.isdigit():
            out.add(int(tail))
    return out


def cmd_horizons(args):
    runs = runs_in(pathlib.Path(args.root))
    if not runs:
        raise SystemExit(f"{args.root} に run が無い")
    common = set.intersection(*(exported_steps(d) for _, _, d in runs)) - {0}
    if not common:
        raise SystemExit("全部の run に共通の書き出しが無い（学習が最初の書き出しまで進んでいない）")
    steps = sorted(common)
    n = min(args.count, len(steps))
    # 最後の時点を必ず入れて、等間隔に近い時点を選ぶ
    picked = sorted({steps[round((i + 1) * len(steps) / n) - 1] for i in range(n)})
    behind = {d.name: max(exported_steps(d)) for _, _, d in runs if max(exported_steps(d)) > steps[-1]}
    print(json.dumps(picked))
    print(f"共通の書き出し {steps[0]}〜{steps[-1]}（{len(steps)} 個）から {picked} を比べる", file=sys.stderr)
    if behind:
        print(f"共通の最後より先まで進んだ run: {behind}", file=sys.stderr)


def strategy_name(path: pathlib.Path) -> str:
    return f"open:book:rl_policy:{path.resolve()}"


def cmd_crossplay(args):
    root = pathlib.Path(args.root)
    nets = [(f"{arm}_s{seed}", d / f"current_{args.step}.bin") for arm, seed, d in runs_in(root)]
    nets = [(name, p) for name, p in nets if p.exists()]
    if len(nets) < 2:
        raise SystemExit(f"更新 {args.step} の重みが 2本に満たない")
    refs = [r for r in args.refs.split() if r]
    by_strategy = {strategy_name(p): name for name, p in nets}
    by_strategy.update({r: f"ref:{r}" for r in refs})
    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".one.jsonl")
    rows = []
    for i, (name, path) in enumerate(nets):
        opponents = [strategy_name(p) for _, p in nets[i + 1:]] + refs
        if not opponents:
            continue
        env = dict(os.environ, ARENA_JSON=str(tmp), ARENA_MATCH_SEED=str(args.match_seed + args.step))
        cmd = [args.arena, str(args.games), strategy_name(path), *opponents]
        r = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if r.returncode != 0:
            print(r.stdout[-3000:], file=sys.stderr)
            raise SystemExit(f"arena が失敗した（{name}）")
        for line in tmp.read_text().splitlines():
            x = json.loads(line)
            rows.append({"step": args.step, "a": name, "b": by_strategy[x["baseline"]], "games": x["games"],
                         "wins_a": x["wins_a"], "wins_b": x["wins_b"], "draws": x["draws"],
                         "checkmate": x["checkmate"], "foul_limit": x["foul_limit"], "max_plies": x["max_plies"],
                         "total_plies": x["total_plies"], "fouls_a": x["fouls_a"], "fouls_b": x["fouls_b"]})
        print(f"[crossplay] 更新 {args.step}: {name} vs {len(opponents)} 体（{i + 1}/{len(nets)}）", flush=True)
    tmp.unlink(missing_ok=True)
    out_path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def arm_of(name: str) -> str:
    return name.rpartition("_s")[0]


def rate(row, side="a"):
    """引き分けは半分に数える"""
    w = row["wins_a"] if side == "a" else row["wins_b"]
    return (w + 0.5 * row["draws"]) / row["games"] if row["games"] else 0.5


def pair_table(rows):
    """{(名前, 名前): 前者の勝率}（両向き）"""
    t = {}
    for r in rows:
        if r["b"].startswith("ref:"):
            continue
        t[(r["a"], r["b"])] = rate(r, "a")
        t[(r["b"], r["a"])] = rate(r, "b")
    return t


def cross_mean(t, xs, ys):
    """xs の各 run の、ys の各 run への勝率の平均（同じ run どうしは除く）"""
    v = [t[(x, y)] for x in xs for y in ys if x != y and (x, y) in t]
    return sum(v) / len(v) if v else float("nan")


def bootstrap_ci(t, xs, ys, rng):
    """両方の腕の seed を復元抽出して平均を取り直す（95% の区間）"""
    vals = []
    for _ in range(BOOTSTRAP):
        bx = [rng.choice(xs) for _ in xs]
        by = [rng.choice(ys) for _ in ys]
        v = [t[(x, y)] for x in bx for y in by if x != y and (x, y) in t]
        if v:
            vals.append(sum(v) / len(v))
    vals.sort()
    return (vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals)) - 1]) if vals else (float("nan"), float("nan"))


def permutation_p(t, xs, ys, rng):
    """腕の名札を入れ替えても同じか（片側。xs のほうが強い向き）。量は「ほかの全部の run への平均勝率」の腕ごとの平均の差"""
    names = xs + ys
    score = {n: (lambda v: sum(v) / len(v) if v else 0.5)([t[(n, m)] for m in names if m != n and (n, m) in t])
             for n in names}

    def diff(group):
        rest = [n for n in names if n not in group]
        return sum(score[n] for n in group) / len(group) - sum(score[n] for n in rest) / len(rest)

    obs = diff(set(xs))
    k = len(xs)
    total = 1
    for i in range(k):
        total = total * (len(names) - i) // (i + 1)
    if total <= PERMUTATIONS:
        groups = [set(c) for c in itertools.combinations(names, k)]
    else:
        groups = [set(rng.sample(names, k)) for _ in range(PERMUTATIONS)]
    ge = sum(1 for g in groups if diff(g) >= obs - 1e-12)
    return ge / len(groups), obs


def pct(x):
    return "–" if x != x else f"{100 * x:.1f}%"


def cmd_table(args):
    files = sorted(pathlib.Path(args.results).glob("*.jsonl"))
    rows = [json.loads(line) for f in files for line in f.read_text().splitlines() if line.strip()]
    if not rows:
        raise SystemExit(f"{args.results} に結果が無い")
    steps = sorted({r["step"] for r in rows})
    rng = random.Random(20261122)
    out = []
    verdict = None
    for step in steps:
        rs = [r for r in rows if r["step"] == step]
        t = pair_table(rs)
        names = sorted({n for pair in t for n in pair})
        arms = sorted({arm_of(n) for n in names})
        by_arm = {a: [n for n in names if arm_of(n) == a] for a in arms}
        games = rs[0]["games"]
        out.append(f"### {step} 更新（1更新 64局 = 通算 {step * 64:,} 局。1対 {games} 局）\n")
        out.append("行の腕の、列の腕への勝率（seed の組の平均。かっこ内は seed を取り直したときの 95% の幅）。"
                   "対角は同じ入力の seed どうし。\n")
        out.append("| | " + " | ".join(f"vs {a}（{len(by_arm[a])}本）" for a in arms) + " |")
        out.append("| --- |" + " --- |" * len(arms))
        for a in arms:
            cells = []
            for b in arms:
                m = cross_mean(t, by_arm[a], by_arm[b])
                if a == b:
                    # 同じ腕の中: 平均は必ず 50% なので、seed ごとの「同じ腕のほかの seed への勝率」の散らばりを出す
                    per = [cross_mean(t, [n], [x for x in by_arm[a] if x != n]) for n in by_arm[a]]
                    per = [p for p in per if p == p]
                    cells.append(f"seed ごと {pct(min(per))}〜{pct(max(per))}" if per else "–")
                else:
                    lo, hi = bootstrap_ci(t, by_arm[a], by_arm[b], rng)
                    cells.append(f"**{pct(m)}**（{pct(lo)}〜{pct(hi)}）")
            out.append(f"| {a} | " + " | ".join(cells) + " |")
        out.append("")
        # 基準の相手（絶対的な強さの目安）と、反則・手数
        refs = sorted({r["b"] for r in rs if r["b"].startswith("ref:")})
        if refs:
            out.append("| 腕 | " + " | ".join(f"{r[4:]} への勝率" for r in refs) + " | 1局の反則（腕どうしの対局） | 平均手数 |")
            out.append("| --- |" + " --- |" * (len(refs) + 2))
            for a in arms:
                cells = []
                for ref in refs:
                    v = [rate(r, "a") for r in rs if r["b"] == ref and arm_of(r["a"]) == a]
                    cells.append(pct(sum(v) / len(v)) if v else "–")
                fouls = [f / r["games"] for r in rs if not r["b"].startswith("ref:")
                         for side, f in ((r["a"], r["fouls_a"]), (r["b"], r["fouls_b"])) if arm_of(side) == a]
                plies = [r["total_plies"] / r["games"] for r in rs
                         if not r["b"].startswith("ref:") and a in (arm_of(r["a"]), arm_of(r["b"]))]
                cells.append(f"{sum(fouls) / len(fouls):.2f}" if fouls else "–")
                cells.append(f"{sum(plies) / len(plies):.0f}" if plies else "–")
                out.append(f"| {a} | " + " | ".join(cells) + " |")
            out.append("")
        # 判定の組（候補 対 対照）を先に、ほかの腕の組も同じ形で出す（腕が 3つ以上のとき。名前の後ろの腕が候補の側）
        pairs = [(args.treat, args.control)] + [(b, a) for i, a in enumerate(arms) for b in arms[i + 1:]
                                                if {a, b} != {args.treat, args.control}]
        for x_arm, y_arm in pairs:
            if x_arm not in by_arm or y_arm not in by_arm:
                continue
            xs, ys = by_arm[x_arm], by_arm[y_arm]
            m = cross_mean(t, xs, ys)
            lo, hi = bootstrap_ci(t, xs, ys, rng)
            p, d = permutation_p(t, xs, ys, rng)
            out.append(f"**{x_arm} 対 {y_arm}**: {pct(m)}（{pct(lo)}〜{pct(hi)}）。"
                       f"名札の入れ替え検定（片側）p = {p:.3f}（この 2つの腕の全 run への平均勝率の差 {100 * d:+.1f}pt）\n")
            if (x_arm, y_arm) == (args.treat, args.control):
                verdict = (step, m, lo, hi, p, len(xs), len(ys))
    head = ["## 入力の A/B（小さい自己対局）\n"]
    if verdict:
        step, m, lo, hi, p, nx, ny = verdict
        if lo > 0.5 and p < 0.05:
            word = f"**{args.treat} が {args.control} より強い**と言える"
        elif hi < 0.5:
            word = f"**{args.treat} は {args.control} より弱い**と言える"
        else:
            word = f"**{args.treat} と {args.control} の差は、この規模では分からない**"
        head.append(f"最後の時点（{step} 更新）の判定: {word}（{args.treat} {nx}本 対 {args.control} {ny}本、"
                    f"勝率 {pct(m)}、seed を取り直したときの幅 {pct(lo)}〜{pct(hi)}、入れ替え検定 p = {p:.3f}）。\n")
    out = head + out
    if args.depend:
        dep = [x for f in args.depend for x in json.loads(pathlib.Path(f).read_text())]
        if dep:
            out.append("### 入力の組をどれだけ読んでいるか\n")
            out.append("その組のプレーンを 0 にして渡したときの方策の変化（全変動距離。0 = 読んでいない、1 = 手が全部変わる）と、"
                       "最善手が変わる割合。組に値がある手番だけで数えた、seed の平均。\n")
            out.append("| 腕 | 更新 | 組 | 値がある手番 | 方策の変化 | 最善手が変わる | seed ごとの方策の変化 |")
            out.append("| --- | --- | --- | --- | --- | --- | --- |")
            keys = sorted({(x["arm"], x["step"], x["group"]) for x in dep})
            for arm, step, group in keys:
                xs = [x for x in dep if (x["arm"], x["step"], x["group"]) == (arm, step, group)]
                tv = [x["tv_nonzero"] for x in xs if x["tv_nonzero"] is not None]
                fl = [x["flip_nonzero"] for x in xs if x["flip_nonzero"] is not None]
                nz = [x["nonzero"] for x in xs]
                if not tv:
                    continue
                out.append(f"| {arm} | {step} | {group} | {pct(sum(nz) / len(nz))} | {sum(tv) / len(tv):.3f} | "
                           f"{pct(sum(fl) / len(fl))} | {min(tv):.3f}〜{max(tv):.3f} |")
            out.append("")
    print("\n".join(out))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("plan", help="起動の引数を解釈する")
    pl.add_argument("--request", default="", help="リクエストファイル（無ければ環境変数 IN_<キー> = 手動起動の入力）")
    pl.set_defaults(func=cmd_plan)
    m = sub.add_parser("merge", help="学習の段ごとの artifact から、run ごとにいちばん進んだものを集める")
    m.add_argument("src", help="artifact ごとのディレクトリが並ぶ場所")
    m.add_argument("dst")
    m.set_defaults(func=cmd_merge)
    h = sub.add_parser("horizons", help="比べる時点を選ぶ")
    h.add_argument("root")
    h.add_argument("--count", type=int, default=4)
    h.set_defaults(func=cmd_horizons)
    c = sub.add_parser("crossplay", help="ある更新の重みを総当たりにする")
    c.add_argument("root")
    c.add_argument("--step", type=int, required=True)
    c.add_argument("--games", type=int, default=100, help="1対の局数（偶数。先後を半分ずつ）")
    c.add_argument("--arena", required=True, help="arena のバイナリ")
    c.add_argument("--out", required=True)
    c.add_argument("--refs", default="", help="基準の相手（スペース区切りの戦略名。絶対的な強さの目安）")
    c.add_argument("--match-seed", type=int, default=20261122)
    c.set_defaults(func=cmd_crossplay)
    t = sub.add_parser("table", help="表と判定")
    t.add_argument("results", help="crossplay の出力（*.jsonl）のディレクトリ")
    t.add_argument("--control", required=True)
    t.add_argument("--treat", required=True)
    t.add_argument("--depend", nargs="*", default=[], help="tsuitate-nn の input_ab.py depend の出力")
    t.set_defaults(func=cmd_table)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
