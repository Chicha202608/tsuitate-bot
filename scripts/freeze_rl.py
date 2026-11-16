#!/usr/bin/env python3
"""方策ネット（DeepNash 路線の R-NaD）の凍結版を生成する。

    python3 scripts/freeze_rl.py <N> <日付> "<要約>" models/rl_vN.bin > src/frozen/rl_vN.rs
    python3 scripts/freeze_rl.py <N> <日付> "<要約>" models/rl_<系列>_vN.bin --series <系列> > src/frozen/rl_<系列>_vN.rs

`--series` は本線以外の系列（スタイル特化モデル。例 `nyugyoku` → `rl_nyugyoku_vN` / `RlNyugyokuVN`）。
省略すると本線の `rl_vN`。

`--open-book <定跡ファイルの固定コピー>` は **序盤の定跡を内蔵した凍結版**（2026-11-17、rl_scratch_v1 から）。
序盤を 100% 強制して学習したネットは初期局面から自分で指したことが無いので、凍結版が毎局ブックから1本引いて
接触までそのラインを指す（`open:book:rl_policy:<重み>` と同じ seed なら同じライン・同じ手）。定跡は
`models/<名前>.openings.json` に固定コピーを置いて埋め込み、強制の仕組み（`src/rl/forced.rs` のブック・予定・包み）も
固定コピーで持つ（`rl_openings.json` や `forced.rs` を後から変えても凍結版は動かない）。

凍結版は **推論の一式を固定コピーとして持つ**（estimator の凍結と同じ考え方）:
- `src/rl/action.rs`（行動の符号化・マスク）・`src/rl/encode.rs`（観測のテンソル化）・
  `src/rl/policy_net.rs`（手書きの推論）をテストを落として部分モジュールへ入れる。
  以後 `src/rl/` を改良しても凍結版の挙動は変わらない
- テンソル化が使う手数・反則の上限（`selfplay::MAX_PLIES` / `MAX_FOULS`）は凍結時点の値で埋め込む
  （上限を変えると入力の正規化が変わるため）
- 重みは `include_bytes!` でバイナリに埋め込み、sha256 をテストで検査する
- サンプリング（softmax から seed つきで引く）は `src/rl/policy.rs` の `rl_policy:` と同じ手順
  （マスクは凍結時点の `action::legal_mask`。rl_v22 以前は旧マスク＝`rl_policy_basic:` と同じ）
- 共有のまま使うのはルールエンジン（board / shogi）・観測（observation）・自駒の再構成（model）・
  観測からの演繹（deduce。エンコーダ v2 = 確定事実のプレーンつきの重みだけが呼ぶ）。
  model と deduce は `frozen::SHARED_MODEL_PINS` で pin する（変えるとテンソルが変わるため）
- 実行時 env は読まない（`frozen::HERMETIC_FROM` 以降の規約）

同一性の確認: この スクリプトを再実行して凍結ファイルとの diff が無いこと、CI で
「元の重みの `rl_policy:release:...` vs `rl_vN`」が 50%±10 に入ること。
"""

import hashlib
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def body(path: str) -> str:
    src = (ROOT / path).read_text()
    # テストは末尾の #[cfg(test)] mod tests { ... } だけ（落とす）
    i = src.find("\n#[cfg(test)]\nmod tests")
    if i >= 0:
        src = src[:i] + "\n"
    # モジュール先頭の //! doc は部分モジュールでは // にする（内側の doc として残す）
    src = re.sub(r"^//!", "//", src, flags=re.M)
    src = src.replace("crate::rl::action::", "super::action::")
    src = src.replace("crate::rl::encode::", "super::encode::")
    src = src.replace("crate::rl::policy_net::", "super::policy_net::")
    return src


def indent(src: str) -> str:
    return "".join(("    " + line if line.strip() else line) for line in src.splitlines(True))


def drop_items(src: str, heads) -> str:
    """トップレベルの項目（直前の /// の doc ごと）を落とす。`heads` は項目の最初の行の先頭"""
    lines = src.split("\n")
    for head in heads:
        idx = [i for i, l in enumerate(lines) if l.startswith(head)]
        if len(idx) != 1:
            sys.exit(f"forced.rs の項目 {head!r} が {len(idx)} 個ある（1個のはず）")
        s = e = idx[0]
        while s > 0 and lines[s - 1].startswith("///"):
            s -= 1
        if not lines[e].rstrip().endswith(";"):
            while lines[e] != "}":
                e += 1
        del lines[s:e + 1]
        if s < len(lines) and lines[s] == "" and s > 0 and lines[s - 1] == "":
            del lines[s]
    return "\n".join(lines)


def forced_body() -> str:
    """序盤の強制（`src/rl/forced.rs`）のうち、ブック・予定・包みだけの固定コピー。
    本番bot の env・`open:` の戦略名・学習用のブック（`rl_openings.json` の埋め込み）は落とす"""
    src = drop_items(body("src/rl/forced.rs"), [
        "pub fn bot_book_from_env", "pub fn wrap_for_game", "pub const OPEN_PREFIX", "fn training_book",
        "pub fn fixed_opening_from_name", "fn plan_from_name", "pub fn opening_line_from_name",
        "pub fn training_book_lines",
    ])
    mix = re.search(r"pub fn mix\(mut z: u64\) -> u64 \{\n.*?\n\}\n", (ROOT / "src/selfplay.rs").read_text(), re.S)
    if not mix or src.count("use crate::selfplay::mix;\n") != 1:
        sys.exit("selfplay::mix の固定コピーを作れない")
    src = src.replace("use crate::selfplay::mix;\n", "")
    src = src.rstrip("\n") + "\n"
    src += "\n// 乱数の混ぜ方（`selfplay::mix` の凍結時点の固定コピー）\n" + mix.group(0).replace("pub fn mix", "fn mix")
    for bad in ("env::var(", "crate::rl::", "crate::selfplay::", "include_str!", "strategy::make"):
        if bad in src:
            sys.exit(f"forced の固定コピーに凍結されない依存が残っている（{bad}）")
    return src


def with_opening(out: str, ty: str, strat: str, label: str, open_book: str) -> str:
    """序盤の定跡を内蔵した形にする: 方策の型を `<型>Net` に下げ、`<型>` はそれを強制の包みで包む"""
    def rep(a: str, b: str) -> None:
        nonlocal out
        if out.count(a) != 1:
            sys.exit(f"凍結版の雛形に {a!r} が {out.count(a)} 個ある（1個のはず）")
        out = out.replace(a, b)

    book_sha = hashlib.sha256((ROOT / open_book).read_bytes()).hexdigest()
    for head in ("pub struct ", "impl ", "impl Default for ", "impl Strategy for "):
        rep(f"\n{head}{ty} {{", f"\n{head}{ty}Net {{")
    rep(f"/// {label} の戦略（`strategy::make(\"{strat}\")`）。softmax からサンプリングする\n",
        f"/// {label} の方策（序盤の強制の中身。softmax からサンプリングする）\n")
    rep("//! 探索なしで1手 約10ms。",
        f"//! **序盤の定跡を内蔵する**: 毎局 `{open_book}`（凍結時点の `rl_openings.json` の固定コピー、\n"
        f"//! sha256 `{book_sha}`）から系統ごとに同じ確率で1本引き、接触・王手・自分の反則まで\n"
        "//! そのラインを指す（`open:book:rl_policy:<重み>` と同じ seed なら同じライン・同じ手）。序盤を 100% 強制して\n"
        "//! 学習したネットは初期局面から自分で指したことが無いため。強制の仕組みも固定コピー（`forced`）。\n"
        "//! 探索なしで1手 約10ms。")
    wrapper = f"""/// 埋め込んだ定跡（凍結時点の `rl_openings.json` の固定コピー）
const OPENINGS: &str = include_str!("../../{open_book}");
/// 埋め込んだ定跡の sha256（凍結時点の値。テストで検査する）
pub const OPENINGS_SHA256: &str = "{book_sha}";

fn book() -> &'static forced::ForcedBook {{
    static BOOK: OnceLock<forced::ForcedBook> = OnceLock::new();
    BOOK.get_or_init(|| {{
        forced::book_from_json(OPENINGS, 1.0, None, forced::ForcedSides::Both).expect("凍結版の定跡が読めない")
    }})
}}

/// {label} の戦略（`strategy::make("{strat}")`）。序盤は内蔵の定跡から1本引いて指し、以後は方策に任せる
pub struct {ty}(forced::ForcedOpening<{ty}Net>);

impl {ty} {{
    pub fn new() -> Self {{
        Self::with_seed(rand::rng().random())
    }}

    /// ラインも方策の乱数も `seed` から決まる（`open:book:rl_policy:<重み>` に同じ seed を渡したときと同じ）
    pub fn with_seed(seed: u64) -> Self {{
        let plan = forced::book_plan(book(), seed).expect("p = 1 のブックは必ず予定が付く");
        Self(forced::ForcedOpening::new(Box::new({ty}Net::with_seed(seed)), plan))
    }}
}}

impl Default for {ty} {{
    fn default() -> Self {{
        Self::new()
    }}
}}

impl Strategy for {ty} {{
    fn choose(
        &mut self,
        view: &PlayerView,
        log: &ObservationLog,
        foul_tried: &HashSet<String>,
    ) -> Option<String> {{
        self.0.choose(view, log, foul_tried)
    }}

    fn name(&self) -> &'static str {{
        "{strat}"
    }}

    fn debug_state(&self) -> Option<serde_json::Value> {{
        self.0.debug_state()
    }}
}}

/// 序盤の強制（`src/rl/forced.rs` のブック・予定・包みの凍結時点の固定コピー）
pub mod forced {{
{indent(forced_body())}}}

"""
    anchor = "/// 行動の符号化とマスク（`src/rl/action.rs` の凍結時点の固定コピー）\npub mod action {"
    rep(anchor, wrapper + anchor)
    rep("    fn 埋め込んだ重みは凍結時点のもの() {", """    fn 埋め込んだ定跡は凍結時点のもの() {
        use sha2::{Digest, Sha256};
        let got: String = Sha256::digest(OPENINGS.as_bytes()).iter().map(|b| format!("{b:02x}")).collect();
        assert_eq!(got, OPENINGS_SHA256);
        assert!(book().line_lengths().iter().all(|&n| n > 0));
    }

    #[test]
    fn 埋め込んだ重みは凍結時点のもの() {""")
    return out


def main() -> None:
    args = sys.argv[1:]
    series = ""
    if "--series" in args:
        i = args.index("--series")
        series = args[i + 1]
        del args[i:i + 2]
        if not re.fullmatch(r"[a-z][a-z0-9]*", series):
            sys.exit(f"--series は英小文字と数字だけ（{series}）")
    open_book = ""
    if "--open-book" in args:
        i = args.index("--open-book")
        open_book = args[i + 1]
        del args[i:i + 2]
    if len(args) != 4:
        sys.exit(__doc__)
    n, date, summary, weights = args
    # 戦略名と型名: 本線は rl_vN / RlVN、系列つきは rl_<系列>_vN / Rl<系列>VN
    strat = f"rl_{series}_v{n}" if series else f"rl_v{n}"
    ty = f"Rl{series.capitalize()}V{n}" if series else f"RlV{n}"
    label = f"{series} 系列の凍結版 v{n}" if series else f"凍結版 v{n}"
    data = (ROOT / weights).read_bytes()
    sha = hashlib.sha256(data).hexdigest()

    selfplay = (ROOT / "src/selfplay.rs").read_text()
    max_fouls = re.search(r"pub const MAX_FOULS: u32 = (\d+);", selfplay).group(1)
    max_plies = re.search(r"pub const MAX_PLIES: u32 = (\d+);", selfplay).group(1)

    action = body("src/rl/action.rs")
    encode = body("src/rl/encode.rs").replace(
        "use crate::selfplay::{MAX_FOULS, MAX_PLIES};",
        f"// 凍結時点の手数・反則の上限（入力の正規化に使う）\n"
        f"const MAX_FOULS: u32 = {max_fouls};\nconst MAX_PLIES: u32 = {max_plies};",
    )
    policy_net = body("src/rl/policy_net.rs")
    for name, src in (("action", action), ("encode", encode), ("policy_net", policy_net)):
        if "env::var(" in src:
            sys.exit(f"{name} が実行時 env を読んでいる（凍結版は読まない）")
        if "crate::rl::" in src or "crate::selfplay::" in src:
            sys.exit(f"{name} に凍結されない依存が残っている")

    rel_weights = "../../" + weights
    out = f"""//! **{label}**（{date}）: 方策ネット（DeepNash 路線の R-NaD）。{summary}
//!
//! `scripts/freeze_rl.py` が生成した。**編集しない**（改善は `src/rl/` で行う）。
//! 推論の一式（行動の符号化・観測のテンソル化・手書きの推論）は凍結時点の固定コピーで、
//! 重みは `{weights}` をバイナリに埋め込む（sha256 `{sha}`）。
//! 探索なしで1手 約10ms。思考予算・実行時 env は持たない。

#![allow(clippy::all)]

use std::collections::HashSet;
use std::sync::OnceLock;

use rand::{{Rng, SeedableRng, rngs::StdRng}};

use crate::observation::ObservationLog;
use crate::protocol::PlayerView;
use crate::strategy::Strategy;

/// 埋め込んだ重み
const WEIGHTS: &[u8] = include_bytes!("{rel_weights}");
/// 埋め込んだ重みの sha256（凍結時点の値。テストで検査する）
pub const WEIGHTS_SHA256: &str = "{sha}";

fn net() -> &'static policy_net::PolicyNet {{
    static NET: OnceLock<policy_net::PolicyNet> = OnceLock::new();
    NET.get_or_init(|| policy_net::PolicyNet::from_bytes(WEIGHTS).expect("凍結版の重みが読めない"))
}}

/// {label} の戦略（`strategy::make("{strat}")`）。softmax からサンプリングする
pub struct {ty} {{
    rng: StdRng,
    last: Option<serde_json::Value>,
}}

impl {ty} {{
    pub fn new() -> Self {{
        Self {{ rng: StdRng::from_rng(&mut rand::rng()), last: None }}
    }}

    pub fn with_seed(seed: u64) -> Self {{
        Self {{ rng: StdRng::seed_from_u64(seed), last: None }}
    }}
}}

impl Default for {ty} {{
    fn default() -> Self {{
        Self::new()
    }}
}}

impl Strategy for {ty} {{
    fn choose(
        &mut self,
        view: &PlayerView,
        log: &ObservationLog,
        foul_tried: &HashSet<String>,
    ) -> Option<String> {{
        // src/rl/policy.rs の rl_policy（サンプリング）と同じ手順
        let mask = action::legal_mask(view, log, foul_tried);
        let legal: Vec<usize> = (0..mask.len()).filter(|&a| mask[a]).collect();
        if legal.is_empty() {{
            return None;
        }}
        let (logits, value) = net().forward(&encode::encode_planes(net().planes(), view, log, foul_tried));
        let max = legal.iter().map(|&a| logits[a]).fold(f32::MIN, f32::max);
        let weights: Vec<f64> = legal.iter().map(|&a| f64::from(logits[a] - max).exp()).collect();
        let total: f64 = weights.iter().sum();
        let mut r = self.rng.random::<f64>() * total;
        let mut pick = *legal.last().unwrap();
        for (&a, w) in legal.iter().zip(&weights) {{
            if r < *w {{
                pick = a;
                break;
            }}
            r -= w;
        }}
        let prob = weights[legal.iter().position(|&a| a == pick).unwrap()] / total;
        self.last = Some(serde_json::json!({{ "p": prob, "value": value }}));
        action::decode_usi(pick, view.your_color)
    }}

    fn name(&self) -> &'static str {{
        "{strat}"
    }}

    fn debug_state(&self) -> Option<serde_json::Value> {{
        self.last.clone()
    }}
}}

/// 行動の符号化とマスク（`src/rl/action.rs` の凍結時点の固定コピー）
pub mod action {{
{indent(action)}}}

/// 観測のテンソル化（`src/rl/encode.rs` の凍結時点の固定コピー）
pub mod encode {{
{indent(encode)}}}

/// 手書きの推論（`src/rl/policy_net.rs` の凍結時点の固定コピー）
pub mod policy_net {{
{indent(policy_net)}}}

#[cfg(test)]
mod tests {{
    use super::*;

    #[test]
    fn 埋め込んだ重みは凍結時点のもの() {{
        use sha2::{{Digest, Sha256}};
        let got: String = Sha256::digest(WEIGHTS).iter().map(|b| format!("{{b:02x}}")).collect();
        assert_eq!(got, WEIGHTS_SHA256);
        assert!(net().forward(&vec![0.0; net().planes() * 81]).0.len() == action::NUM_ACTIONS);
    }}
}}"""
    if open_book:
        out = with_opening(out, ty, strat, label, open_book)
    print(out)


if __name__ == "__main__":
    main()
