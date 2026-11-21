//! 相手玉の位置候補を、相手の1手ごとの「玉が動けたか」の手掛かりで締めた版（2026-11-21）。
//!
//! 元の版は `deduce::opp_king_candidates`。あちらは凍結版（estimator_v8 以降と、確定事実のプレーンを使う方策ネット）が
//! 共有していて、`frozen::SHARED_MODEL_PINS` がファイルごと sha256 で pin している。挙動を変えられないので、締めた版は
//! このファイルに分けてある。下請けの関数（自駒だけの盤・確実な利き・新たに利きが生じ得たマス）は deduce.rs からの写し

use std::collections::HashMap;

use crate::board::{Coord, on_board};
use crate::deduce::immobile_opponent_pawns;
use crate::observation::{Observation, ObservationLog};
use crate::protocol::{Color, Role};
use crate::shogi::{Position, ShogiMove};

/// `opp_king_candidates` を、相手の1手ごとの「玉が動けたか」の手掛かりで締めた版（2026-11-21 ユーザーとの議論）。
/// 元の版は相手が1手指すたびに候補を1マスずつ広げる。こちらは観測から分かる次の3つを使う。どれも健全
/// （真の玉を落とさない）で、落とすのは「その手で玉が動けなかった」場合だけ:
///
/// 1. **相手が駒を取った手**: 動いた駒は取ったマス X にいる。玉が動いたなら玉は X にいるので、候補は
///    「動かなかった」か「X（隣の候補があるとき）」だけ
/// 2. **相手がこちらへ王手をかけた手**: 玉は自分では王手をかけられない。玉が動いて王手になるのは開き王手
///    だけなので、玉が自玉と同じ筋・段・斜めにいて、間に自駒が無く、玉の後ろに飛び駒が入る余地があり、
///    その線から外れる移動先に限って広げる
/// 3. **居ると分かっていた相手の駒が動いていた**: 駒を取った相手の駒（取ったマスにいる）と、王手を受けて
///    いない手番の歩以外の打ちの反則（打ち先に相手の駒がいる）で相手の駒の居場所が分かる。あとで自分の
///    手がそのマスに駒を取らずに着地した・通り抜けた・打てたなら、その駒は間のどれか1手で動いている。
///    その駒が玉でなければ、その1手で玉は動いていない。玉だった場合（そのマスが玉の候補だったとき）は
///    玉がそのマスにいたことになる。両方の場合の候補の和で絞る
///
/// 元の版（`crate::deduce::KingCandidates`）は凍結版のエンコーダ（確定事実の `D_KING`）が共有しているので変えない
pub fn opp_king_candidates_tight(my_color: Color, log: &ObservationLog) -> std::collections::BTreeSet<Coord> {
    let mut king = KingCandidatesTight::new(my_color);
    king.advance(log);
    king.candidates(log, &immobile_opponent_pawns(my_color, log))
}

/// 居場所が分かった相手の駒（`KingCandidatesTight` の規則 3）
#[derive(Clone)]
struct KnownPiece {
    sq: Coord,
    /// この駒が玉でなく、分かった後の相手の手のどれか1手でもう動いた、としたときの玉の候補
    moved: std::collections::BTreeSet<Coord>,
    /// この駒が玉だった、としたときの玉の候補
    king: std::collections::BTreeSet<Coord>,
}

/// `opp_king_candidates_tight` の途中状態（使い方は `crate::deduce::KingCandidates` と同じ）
#[derive(Clone)]
pub struct KingCandidatesTight {
    my_color: Color,
    model: crate::model::GameModel,
    cands: std::collections::BTreeSet<Coord>,
    checking_board: Option<Position>,
    known: Vec<KnownPiece>,
    /// 自分が王手を受けている（打ちの反則の原因を決めるのに使う）
    in_check: bool,
    done: usize,
    gave_up: bool,
    rules: u8,
}

/// 規則 3 で同時に追う相手の駒の数（古いものから捨てる）
const MAX_KNOWN: usize = 8;

impl KingCandidatesTight {
    pub const RULE_CAPTURE: u8 = 1;
    pub const RULE_CHECK: u8 = 2;
    pub const RULE_MOVED: u8 = 4;

    pub fn new(my_color: Color) -> Self {
        Self::with_rules(my_color, Self::RULE_CAPTURE | Self::RULE_CHECK | Self::RULE_MOVED)
    }

    /// 使う規則を選ぶ（規則ごとの効きを測るため。0 なら `KingCandidates` と同じ候補になる）
    pub fn with_rules(my_color: Color, rules: u8) -> Self {
        let king = Position::initial().king_square(my_color.other());
        Self {
            my_color,
            model: crate::model::GameModel::new(my_color),
            cands: king.into_iter().collect(),
            checking_board: None,
            known: vec![],
            in_check: false,
            done: 0,
            gave_up: king.is_none(),
            rules,
        }
    }

    pub fn done(&self) -> usize {
        self.done
    }

    /// `log` の未処理のイベントを処理する。自分の手も相手の手も直後の王手宣言を見て処理するので、末尾の
    /// 着手は宣言が届くまで処理を保留する（`candidates` が写しの上で処理する）
    pub fn advance(&mut self, log: &ObservationLog) {
        let events = log.events();
        while self.done < events.len() && !self.gave_up {
            let last = self.done + 1 == events.len();
            if last && matches!(events[self.done], Observation::MyMove { .. } | Observation::OpponentMoved { .. }) {
                break;
            }
            self.step(events, self.done);
            self.done += 1;
        }
    }

    pub fn candidates(
        &self,
        log: &ObservationLog,
        locked: &std::collections::BTreeSet<Coord>,
    ) -> std::collections::BTreeSet<Coord> {
        let events = log.events();
        let mut cands = if self.gave_up {
            return all_squares();
        } else if let Some(next) = self.peek_opponent_move(events) {
            next
        } else if self.done < events.len() {
            // 保留した末尾の着手を写しの上で処理する
            let mut t = self.clone();
            while t.done < events.len() && !t.gave_up {
                t.step(events, t.done);
                t.done += 1;
            }
            if t.gave_up {
                return all_squares();
            }
            t.cands
        } else {
            self.cands.clone()
        };
        if !locked.is_empty() {
            let pruned: std::collections::BTreeSet<Coord> = cands.difference(locked).copied().collect();
            if !pruned.is_empty() {
                cands = pruned;
            }
        }
        cands
    }

    /// 保留しているのが末尾の相手の手1つだけ（自分の手番の頭のふつうの形）なら、その手の後の候補を状態を
    /// 変えずに出す。末尾なので王手宣言は無い。結果は `step` で処理したときと同じ
    fn peek_opponent_move(&self, events: &[Observation]) -> Option<std::collections::BTreeSet<Coord>> {
        if self.done + 1 != events.len() {
            return None;
        }
        let Observation::OpponentMoved { captured_my_piece_at, .. } = &events[self.done] else {
            return None;
        };
        let captured_sq = captured_my_piece_at
            .as_deref()
            .and_then(crate::board::parse_usi_square);
        let mut board = my_only_board(&self.model);
        if let Some(x) = captured_sq {
            board.set(x, None);
        }
        let st = OppStep {
            my_color: self.my_color,
            board: &board,
            checking_board: self.checking_board.as_ref(),
            captured_sq,
            use_capture: self.rules & Self::RULE_CAPTURE != 0,
            discover: None,
        };
        let next = st.normal(&self.cands);
        Some(if next.is_empty() { all_squares() } else { next })
    }

    fn step(&mut self, events: &[Observation], i: usize) {
        use std::collections::BTreeSet;
        let my_color = self.my_color;
        let opp = my_color.other();
        let rules = self.rules;
        let e = &events[i];
        self.model.apply(e);
        match e {
            Observation::MyMove { usi, captured, .. } => {
                let board = my_only_board(&self.model);
                let squares = crate::shogi::parse_usi(usi).map(|mv| match mv {
                    ShogiMove::Board { from, to, .. } => (to, Some(from)),
                    ShogiMove::Drop { to, .. } => (to, None),
                });
                // 規則 3: 居ると分かっていた駒のマスが空だった（駒を取らずに着地した・通り抜けた・打てた）
                if let Some((to, from)) = squares {
                    let cands = &mut self.cands;
                    self.known.retain(|p| {
                        let landed = to == p.sq;
                        let passed = from.is_some_and(|f| between(f, to, p.sq));
                        if landed && captured.is_some() {
                            return false; // そこの駒を取った（同じ駒かは分からない）
                        }
                        if !(landed || passed) {
                            return true;
                        }
                        let narrowed: BTreeSet<Coord> =
                            cands.iter().copied().filter(|k| p.moved.contains(k) || p.king.contains(k)).collect();
                        if !narrowed.is_empty() {
                            *cands = narrowed;
                        }
                        false
                    });
                }
                self.in_check = false;
                let declared = matches!(
                    events.get(i + 1),
                    Some(Observation::Check { in_check }) if *in_check == opp
                );
                let newly = match (declared, squares) {
                    (true, Some((to, from))) => Some(newly_attacked_squares(&board, my_color, to, from, captured.is_some())),
                    _ => None,
                };
                let filter = |set: &mut BTreeSet<Coord>| {
                    if declared {
                        match &newly {
                            Some(newly) => set.retain(|k| newly.contains(k)),
                            None => set.retain(|&k| possibly_attacked(&board, k, my_color)),
                        }
                    } else {
                        set.retain(|&k| !surely_attacked(&board, k, my_color));
                    }
                    set.retain(|&k| board.piece_at(k).is_none());
                };
                let before = self.cands.clone();
                filter(&mut self.cands);
                if self.cands.is_empty() {
                    // 演繹が不健全（か観測が欠けている）。制約を捨てて安全側へ
                    self.cands = before;
                    self.cands.retain(|&k| board.piece_at(k).is_none());
                    self.known.clear();
                    if self.cands.is_empty() {
                        self.gave_up = true;
                        return;
                    }
                }
                for p in &mut self.known {
                    filter(&mut p.moved);
                    filter(&mut p.king);
                }
                self.drop_useless_known();
                self.checking_board = declared.then_some(board);
            }
            Observation::OpponentMoved {
                captured_my_piece_at, ..
            } => {
                let captured_sq = captured_my_piece_at
                    .as_deref()
                    .and_then(crate::board::parse_usi_square);
                let gave_check = matches!(
                    events.get(i + 1),
                    Some(Observation::Check { in_check }) if *in_check == my_color
                );
                let board = my_only_board(&self.model);
                let checking_board = self.checking_board.take();
                let by_check = rules & Self::RULE_CHECK != 0 && gave_check;
                let st = OppStep {
                    my_color,
                    board: &board,
                    checking_board: checking_board.as_ref(),
                    captured_sq,
                    use_capture: rules & Self::RULE_CAPTURE != 0,
                    discover: by_check.then(|| DiscoveredCheck::new(&self.model.my_hand(), &board, my_color)),
                };
                let next = st.normal(&self.cands);
                if next.is_empty() {
                    self.cands = all_squares();
                    self.known.clear();
                } else {
                    let not_king = st.stay(&self.cands);
                    for p in &mut self.known {
                        let mut moved = st.normal(&p.moved);
                        moved.extend(not_king.iter().copied());
                        p.moved = moved;
                        p.king = st.normal(&p.king);
                    }
                    self.cands = next;
                    if let (true, Some(x)) = (rules & Self::RULE_MOVED != 0, captured_sq) {
                        // 駒を取った駒は取ったマスにいる
                        self.known.retain(|p| p.sq != x);
                        let king = self.cands.contains(&x).then_some(x).into_iter().collect();
                        self.push_known(KnownPiece { sq: x, moved: BTreeSet::new(), king });
                    }
                    self.drop_useless_known();
                }
                self.in_check = gave_check;
            }
            Observation::MyFoul { usi, .. } => {
                if rules & Self::RULE_MOVED == 0 || self.in_check {
                    return;
                }
                // 王手を受けていない手番の、歩以外の打ちの反則: 打ち先に相手の駒がいる（二歩・打ち歩詰めは歩だけ。
                // 行き所のないマスと自駒のマスは念のため除く）。持っていない駒の打ちは空きマスでも反則になるので、
                // その駒種を持っていたときに限る（反則は持ち駒を変えないので、いまの持ち駒が打った時点の持ち駒）
                let Some(ShogiMove::Drop { role, to }) = crate::shogi::parse_usi(usi) else {
                    return;
                };
                if self.model.my_hand().get(&role).copied().unwrap_or(0) == 0 {
                    return;
                }
                // 自分から見た段（1 が一番奥）
                let far = if my_color == Color::Sente { to.rank } else { 10 - to.rank };
                let dead_end = match role {
                    Role::Lance => far <= 1,
                    Role::Knight => far <= 2,
                    _ => false,
                };
                let mine = self
                    .model
                    .my_pieces()
                    .iter()
                    .any(|p| crate::board::parse_usi_square(&p.square) == Some(to));
                if role == Role::Pawn || dead_end || mine {
                    return;
                }
                // 前から居ると分かっていたマスなら、いま居ると分かったほうへ置き換える（こちらのほうが絞れる）
                self.known.retain(|p| p.sq != to);
                let king = self.cands.contains(&to).then_some(to).into_iter().collect();
                self.push_known(KnownPiece { sq: to, moved: BTreeSet::new(), king });
            }
            _ => {}
        }
    }

    /// 候補をもう絞れない駒を捨てる。候補が全部 `moved ∪ king` に入っていれば、その駒が動いていたと分かっても
    /// 何も落ちない。候補も `moved`・`king` も同じ操作で進むので、一度そうなれば以後もずっとそうなる（損は無い）
    fn drop_useless_known(&mut self) {
        let cands = &self.cands;
        self.known
            .retain(|p| !cands.iter().all(|k| p.moved.contains(k) || p.king.contains(k)));
    }

    fn push_known(&mut self, p: KnownPiece) {
        if self.known.len() >= MAX_KNOWN {
            self.known.remove(0);
        }
        self.known.push(p);
    }
}

/// 相手の1手ぶんの候補の進め方（`KingCandidatesTight`）
struct OppStep<'a> {
    my_color: Color,
    /// 相手の手の後の、自駒だけの盤
    board: &'a Position,
    /// 直前の自分の手が王手だったときの自駒だけの盤
    checking_board: Option<&'a Position>,
    captured_sq: Option<Coord>,
    /// 規則 1 を使う
    use_capture: bool,
    /// 規則 2 が効く手（こちらへ王手をかけた手）のときだけ
    discover: Option<DiscoveredCheck<'a>>,
}

impl OppStep<'_> {
    /// 直前に掛けた王手の王手駒が隣接していて（＝合駒で防げない）取られてもいないなら、玉は動くしかなかった
    fn forced(&self, k: Coord) -> bool {
        self.checking_board.is_some_and(|b| {
            b.pieces().any(|(from, pc)| {
                pc.color == self.my_color && unblockable(from, k) && b.attacks(from, k) && Some(from) != self.captured_sq
            })
        })
    }

    fn prune(&self, set: &mut std::collections::BTreeSet<Coord>) {
        // 相手は自玉を王手に晒す手を指せない
        set.retain(|&k| !surely_attacked(self.board, k, self.my_color));
        set.retain(|&k| self.board.piece_at(k).is_none());
    }

    /// 玉ではない駒の手だったとき
    fn stay(&self, set: &std::collections::BTreeSet<Coord>) -> std::collections::BTreeSet<Coord> {
        let mut next: std::collections::BTreeSet<Coord> = set.iter().copied().filter(|&k| !self.forced(k)).collect();
        self.prune(&mut next);
        next
    }

    /// どの駒の手か分からないとき
    fn normal(&self, set: &std::collections::BTreeSet<Coord>) -> std::collections::BTreeSet<Coord> {
        let mut next = std::collections::BTreeSet::new();
        for &k in set {
            if !self.forced(k) {
                next.insert(k);
            }
            let can_go = |n: Coord| on_board(n) && self.discover.as_ref().is_none_or(|d| d.possible(k, n));
            match self.captured_sq {
                Some(x) if self.use_capture => {
                    if (k.file - x.file).abs().max((k.rank - x.rank).abs()) == 1 && can_go(x) {
                        next.insert(x);
                    }
                }
                _ => {
                    for df in -1..=1i8 {
                        for dr in -1..=1i8 {
                            let n = Coord { file: k.file + df, rank: k.rank + dr };
                            if (df, dr) != (0, 0) && can_go(n) {
                                next.insert(n);
                            }
                        }
                    }
                }
            }
        }
        self.prune(&mut next);
        next
    }
}

/// 相手玉が動いて開き王手になりうるか（`KingCandidatesTight` の規則 2）。自駒の配置と相手の保有数だけで決める
struct DiscoveredCheck<'a> {
    board: &'a Position,
    my_king: Option<Coord>,
    /// 相手が持ちうる飛び駒（保有数 = 総数 − 自分の盤上 − 自分の持ち駒。成りは元の駒種で数える）
    rook: bool,
    bishop: bool,
    lance: bool,
    /// 相手の香が進む向き（段の増減）
    opp_forward: i8,
}

impl<'a> DiscoveredCheck<'a> {
    /// `board` は相手の手の後の自駒だけの盤、`hand` は自分の持ち駒
    fn new(hand: &HashMap<Role, u32>, board: &'a Position, me: Color) -> Self {
        let has = |role: Role, total: u32| {
            let on_board = board
                .pieces()
                .filter(|(_, p)| p.color == me && crate::shogi::unpromote_role(p.role) == role)
                .count() as u32;
            on_board + hand.get(&role).copied().unwrap_or(0) < total
        };
        Self {
            board,
            my_king: board.king_square(me),
            rook: has(Role::Rook, 2),
            bishop: has(Role::Bishop, 2),
            lance: has(Role::Lance, 4),
            opp_forward: if me == Color::Sente { 1 } else { -1 },
        }
    }

    /// 相手玉が k から n へ動いて、k の後ろにいる飛び駒の利きが自玉へ通りうるか
    fn possible(&self, k: Coord, n: Coord) -> bool {
        let Some(my_king) = self.my_king else { return true };
        let (df, dr) = (k.file - my_king.file, k.rank - my_king.rank);
        if !(df == 0 || dr == 0 || df.abs() == dr.abs()) {
            return false;
        }
        let d = (df.signum(), dr.signum());
        let orth = d.0 == 0 || d.1 == 0;
        // 飛び駒は k の先から自玉へ向かって（−d の向きに）利かせる
        let slider = if orth {
            self.rook || (self.lance && d == (0, -self.opp_forward))
        } else {
            self.bishop
        };
        if !slider {
            return false;
        }
        // 線から外れないと利きは通らない
        let on_line = |c: Coord| c == Coord { file: k.file + d.0, rank: k.rank + d.1 } || c == Coord { file: k.file - d.0, rank: k.rank - d.1 };
        if on_line(n) {
            return false;
        }
        // 自玉と k の間に自駒があれば遮る。k のすぐ後ろが自駒なら飛び駒の入る余地が無い
        let mut c = Coord { file: my_king.file + d.0, rank: my_king.rank + d.1 };
        while c != k {
            if self.board.piece_at(c).is_some() {
                return false;
            }
            c = Coord { file: c.file + d.0, rank: c.rank + d.1 };
        }
        let behind = Coord { file: k.file + d.0, rank: k.rank + d.1 };
        on_board(behind) && self.board.piece_at(behind).is_none()
    }
}

// ---- 以下は deduce.rs の同名の関数の写し（あちらは pin されているので共有にできない） ----

fn all_squares() -> std::collections::BTreeSet<Coord> {
    (1..=9)
        .flat_map(|file| (1..=9).map(move |rank| Coord { file, rank }))
        .collect()
}

/// 自駒だけを置いた盤（未知のマスは空。走り駒のレイはここで最大まで伸びる）
fn my_only_board(model: &crate::model::GameModel) -> Position {
    let me = model.my_color();
    let mut pos = Position::empty(me);
    for p in model.my_pieces() {
        if let Some(sq) = crate::board::parse_usi_square(&p.square) {
            pos.set(sq, Some(crate::shogi::Piece { color: me, role: p.role }));
        }
    }
    pos
}

/// from から to への攻撃が未知の駒に遮られ**得ない**か（隣接1マス or 桂の跳び）
fn unblockable(from: Coord, to: Coord) -> bool {
    let df = (from.file - to.file).abs();
    let dr = (from.rank - to.rank).abs();
    (df <= 1 && dr <= 1) || (df == 1 && dr == 2)
}

/// sq が me の駒に**確実に**利かれているか（遮られ得ない利きだけを数える）
fn surely_attacked(board: &Position, sq: Coord, me: Color) -> bool {
    board
        .pieces()
        .any(|(from, pc)| pc.color == me && unblockable(from, sq) && board.attacks(from, sq))
}

/// sq が me の駒に利き**うる**か（未知のマスを空とみなした上界）
fn possibly_attacked(board: &Position, sq: Coord, me: Color) -> bool {
    board
        .pieces()
        .any(|(from, pc)| pc.color == me && board.attacks(from, sq))
}

/// from から to への直線上（両端を除く）に x があるか。走り駒の「線が開いたか」判定用
fn between(from: Coord, to: Coord, x: Coord) -> bool {
    let (df, dr) = (to.file - from.file, to.rank - from.rank);
    let step = |v: i8| v.signum();
    // 直線（縦横斜め）でなければ通り道は無い
    if !(df == 0 || dr == 0 || df.abs() == dr.abs()) {
        return false;
    }
    let (sf, sr) = (step(df), step(dr));
    let mut c = Coord { file: from.file + sf, rank: from.rank + sr };
    while c != to {
        if c == x {
            return true;
        }
        c = Coord { file: c.file + sf, rank: c.rank + sr };
    }
    false
}

/// 自分の着手によって**新たに**自駒の利きが生じ得るマスの上界。
/// 相手は自分から自玉を王手に晒す手を指せないので、新しい王手は必ず自分の手が作る。
/// 作り方は3通りしかない:
/// - (A) 着手した駒が着地点から利かせる
/// - (B) 出て行ったマス（from）を通る自分の走り駒の線が開く（開き王手）
/// - (C) 取った駒が居たマス（to）を通る自分の走り駒の線が開く
///
/// 集合の**差分**（after で利く ∧ before で利かない）では計算しない: 上界どうしの
/// 差は上界にならず（before 側が過大に主張すると真の新規王手マスを落とす）健全性が壊れる。
/// A/B/C を構成的に足し上げることで上界を保つ
fn newly_attacked_squares(
    after: &Position,
    me: Color,
    to: Coord,
    from: Option<Coord>,
    captured: bool,
) -> std::collections::BTreeSet<Coord> {
    let mut out = std::collections::BTreeSet::new();
    for file in 1..=9i8 {
        for rank in 1..=9i8 {
            let s = Coord { file, rank };
            if s == to {
                continue; // 着地点には自駒が居る
            }
            // (A) 着手した駒の利き
            if after.attacks(to, s) {
                out.insert(s);
                continue;
            }
            // (B)(C) 開いた線
            let opened = after.pieces().any(|(p, pc)| {
                pc.color == me
                    && p != to
                    && after.attacks(p, s)
                    && (from.is_some_and(|f| between(p, s, f)) || (captured && between(p, s, to)))
            });
            if opened {
                out.insert(s);
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::board::make_usi_square;
    use crate::deduce::opp_king_candidates;
    use crate::scenario_core::{load_scenario, replay, scenarios_dir, side_idx};

    fn sq(s: &str) -> Coord {
        crate::board::parse_usi_square(s).unwrap()
    }

    fn set(v: &[&str]) -> std::collections::BTreeSet<Coord> {
        v.iter().map(|s| sq(s)).collect()
    }

    /// **健全性（締めた版）**: 真の玉を落とさず、元の版より広くもならない（2局・全手数・両視点）。
    /// 規則なしは元の版と同じ候補になり、続きだけ食わせた結果・手番の頭の近道の結果は先頭からたどり直した結果と同じ
    #[test]
    fn 締めた敵玉候補は真実を落とさない() {
        for name in ["archive/king-deduction.kif", "archive/quest_20260731.kif"] {
            let path = scenarios_dir().join(name);
            let kifu = load_scenario(&path.to_string_lossy(), Some(0), None, None)
                .unwrap()
                .kifu;
            let mut grown = [KingCandidatesTight::new(Color::Sente), KingCandidatesTight::new(Color::Gote)];
            let mut narrower = 0;
            for ply in 0..=kifu.plies.len() {
                let rep = replay(&kifu, ply);
                for me in [Color::Sente, Color::Gote] {
                    let log = &rep.logs[side_idx(me)];
                    let locked = immobile_opponent_pawns(me, log);
                    let old = opp_king_candidates(me, log);
                    let tight = opp_king_candidates_tight(me, log);
                    let truth = rep.pos.king_square(me.other()).expect("玉");
                    assert!(
                        tight.contains(&truth),
                        "{name} ply={ply} {me:?}視点: 真実 {} が候補{}件に無い",
                        make_usi_square(truth),
                        tight.len()
                    );
                    assert!(tight.is_subset(&old), "{name} ply={ply} {me:?}視点: 元の版より広い");
                    narrower += (tight.len() < old.len()) as usize;
                    let mut none = KingCandidatesTight::with_rules(me, 0);
                    none.advance(log);
                    assert_eq!(none.candidates(log, &locked), old, "{name} ply={ply} {me:?}視点: 規則なし");
                    let g = &mut grown[side_idx(me)];
                    g.advance(log);
                    assert_eq!(g.candidates(log, &locked), tight, "{name} ply={ply} {me:?}視点: 続きだけ食わせた結果");
                    // 末尾の相手の手を、近道でなくふつうに処理させる（後ろに1つ足す）
                    let mut longer = ObservationLog::default();
                    for e in log.events() {
                        longer.record(e.clone());
                    }
                    longer.record(Observation::OpponentFoul { count: 0 });
                    assert_eq!(opp_king_candidates_tight(me, &longer), tight, "{name} ply={ply} {me:?}視点: 近道");
                }
            }
            assert!(narrower > 0, "{name}: 一度も絞れていない");
        }
    }

    /// 規則 1: 相手が駒を取った手では、候補は「動かなかった」か「取ったマス（玉が取った）」だけ
    #[test]
    fn 駒を取った手では取ったマスにしか広げない() {
        let board = Position::empty(Color::Sente);
        let step = |x: Option<&str>| OppStep {
            my_color: Color::Sente,
            board: &board,
            checking_board: None,
            captured_sq: x.map(sq),
            use_capture: true,
            discover: None,
        };
        // 玉の隣で取った: 取ったのが玉かもしれない
        assert_eq!(step(Some("4b")).normal(&set(&["5a"])), set(&["5a", "4b"]));
        // 離れたマスで取った: 玉は動いていない
        assert_eq!(step(Some("2b")).normal(&set(&["5a"])), set(&["5a"]));
        // 駒を取っていない手は今までどおり1マス広げる
        assert_eq!(step(None).normal(&set(&["5a"])), set(&["4a", "4b", "5a", "5b", "6a", "6b"]));
    }

    /// 規則 2: 相手がこちらへ王手をかけた手で玉が動いたなら開き王手。玉が自玉と同じ線にいて、その線から外れる
    /// 移動先にだけ広げる
    #[test]
    fn 王手をかけた手では開き王手になる移動にしか広げない() {
        use crate::shogi::Piece;
        let mine = |role| Some(Piece { color: Color::Sente, role });
        let mut board = Position::empty(Color::Sente);
        board.set(sq("5i"), mine(Role::King));
        let none = HashMap::new();
        let d = DiscoveredCheck::new(&none, &board, Color::Sente);
        // 同じ筋: 後ろ（5b・5a）の飛車か香の利きが通る
        assert!(d.possible(sq("5c"), sq("4c")));
        assert!(d.possible(sq("5c"), sq("6b")));
        assert!(!d.possible(sq("5c"), sq("5b")), "線の上を動いても利きは通らない");
        assert!(!d.possible(sq("5c"), sq("5d")), "線の上を動いても利きは通らない");
        // 斜め: 後ろ（1e）の角の利きが通る
        assert!(d.possible(sq("2f"), sq("2e")));
        assert!(!d.possible(sq("1e"), sq("1d")), "後ろに飛び駒の入るマスが無い");
        assert!(!d.possible(sq("4c"), sq("3c")), "自玉と同じ線にいない");
        // 候補の進め方: 5c の玉は線から外れる6マスへ、4c の玉は動かない
        let step = OppStep {
            my_color: Color::Sente,
            board: &board,
            checking_board: None,
            captured_sq: None,
            use_capture: true,
            discover: Some(DiscoveredCheck::new(&none, &board, Color::Sente)),
        };
        assert_eq!(
            step.normal(&set(&["4c", "5c"])),
            set(&["4b", "4c", "4d", "5c", "6b", "6c", "6d"])
        );
        // 間に自駒があれば遮る
        let mut blocked = board.clone();
        blocked.set(sq("5g"), mine(Role::Pawn));
        assert!(!DiscoveredCheck::new(&none, &blocked, Color::Sente).possible(sq("5c"), sq("4c")));
        // 飛車を2枚とも自分が持っていれば縦横は香だけ。相手の香は自分の側へしか利かない
        let rooks: HashMap<Role, u32> = [(Role::Rook, 2)].into();
        let d = DiscoveredCheck::new(&rooks, &board, Color::Sente);
        assert!(d.possible(sq("5c"), sq("4c")), "後ろの香");
        assert!(!d.possible(sq("2i"), sq("2h")), "横は飛車だけ");
        assert!(d.possible(sq("2f"), sq("2e")), "斜めは角");
        let mut low = Position::empty(Color::Sente);
        low.set(sq("5e"), mine(Role::King));
        assert!(!DiscoveredCheck::new(&rooks, &low, Color::Sente).possible(sq("5g"), sq("4g")), "香は後ろへ利かない");
        // 香も4枚とも自分のものなら縦横は無い
        let all: HashMap<Role, u32> = [(Role::Rook, 2), (Role::Lance, 4)].into();
        assert!(!DiscoveredCheck::new(&all, &board, Color::Sente).possible(sq("5c"), sq("4c")));
    }

    /// 規則 3: 居ると分かっていた相手の駒が動いていたと分かれば、間の1手は玉の手ではない（その駒が玉だった
    /// 場合は、玉がそのマスにいたことになる）
    #[test]
    fn 居ると分かっていた駒が動いていたら絞れる() {
        let mut log = ObservationLog::default();
        let mine = |n: u32, usi: &str| Observation::MyMove {
            move_number: n,
            usi: usi.to_string(),
            captured: None,
        };
        let theirs = |n: u32| Observation::OpponentMoved {
            move_number: n,
            captured_my_piece_at: None,
        };
        log.record(mine(1, "7g7f"));
        log.record(theirs(2));
        // 角を取って持ち駒にする（打ちの反則を「駒がいる」証拠にするには、その駒を持っている必要がある）
        log.record(Observation::MyMove {
            move_number: 3,
            usi: "8h2b+".to_string(),
            captured: Some(Role::Bishop),
        });
        log.record(theirs(4));
        // 王手を受けていない手番の角打ちが反則 = 3c に相手の駒がいる
        log.record(Observation::MyFoul {
            move_number: 5,
            usi: "B*3c".to_string(),
        });
        log.record(mine(5, "2g2f"));
        log.record(theirs(6));
        let before = opp_king_candidates_tight(Color::Sente, &log);
        // 馬の隣（3a・3b・3c）を除いた相手の2手ぶんから、もう1手広げたもの
        assert!(before.len() > 12, "まだ絞れていない: {}件", before.len());
        // 馬が 3c を通り抜けて 4d に着地できた = 3c の駒は相手の直前の1手で動いていた。その1手は玉の手では
        // ないので、玉は1手前（2g2f の後）の候補のまま。そこから 4d の馬の隣（4c・5c）が消える
        log.record(mine(7, "2b4d"));
        let tight = opp_king_candidates_tight(Color::Sente, &log);
        assert_eq!(
            tight,
            set(&["4a", "4b", "5a", "5b", "6a", "6b", "6c", "7a", "7b", "7c"])
        );
        let old = opp_king_candidates(Color::Sente, &log);
        assert!(tight.is_subset(&old) && tight.len() < old.len(), "元の版より絞れる: {}件", old.len());
    }

    /// 規則 3 の健全性: 持っていない駒の打ちは空きマスでも反則になるので、「駒がいる」証拠にしない。
    /// 証拠にすると、後でそのマスへ進めたときに玉が動いていないことにされ、真の玉（3b）を落とす（codex の反例）
    #[test]
    fn 持っていない駒の打ちの反則は駒がいる証拠にしない() {
        let mine = |n: u32, usi: &str| Observation::MyMove {
            move_number: n,
            usi: usi.to_string(),
            captured: None,
        };
        let theirs = |n: u32| Observation::OpponentMoved {
            move_number: n,
            captured_my_piece_at: None,
        };
        // ▲2六歩 △4一玉 ▲2五金打（金を持っていない反則）▲7六歩 △3二玉 ▲2五歩
        let build = |with_foul: bool| {
            let mut log = ObservationLog::default();
            log.record(mine(1, "2g2f"));
            log.record(theirs(2));
            if with_foul {
                log.record(Observation::MyFoul {
                    move_number: 3,
                    usi: "G*2e".to_string(),
                });
            }
            log.record(mine(3, "7g7f"));
            log.record(theirs(4));
            log.record(mine(5, "2f2e"));
            log
        };
        let tight = opp_king_candidates_tight(Color::Sente, &build(true));
        assert!(tight.contains(&sq("3b")), "真の玉 3b が候補{}件に無い", tight.len());
        // 反則が無かったときと同じ候補になる（反則から何も推論しない）
        assert_eq!(tight, opp_king_candidates_tight(Color::Sente, &build(false)));
    }
}
