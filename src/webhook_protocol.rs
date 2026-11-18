//! 「ついたて将棋ビューワー」（tsuboshun氏運営、tsuitate リポジトリとは無関係の
//! 第三者サイト）の webhook bot API契約。
//!
//! 真実は運営者提供のサンプル
//! （<https://github.com/tsuboshun/tsuitate-sample-bot> README「dispatcher からの
//! リクエストモデル」節）。Socket.IO常時接続の protocol.rs とは無関係の
//! 別プロトコルなので型を混ぜない。

use std::collections::HashMap;

use serde::Deserialize;

use crate::protocol::Color;

pub const INFO_NONE: u8 = 0;
pub const INFO_FOUL: u8 = 1;
pub const INFO_FOUL_UNDER_CHECK: u8 = 2;
pub const INFO_CHECK: u8 = 3;
pub const INFO_CHECKMATE: u8 = 4;

pub fn is_foul_info(info: u8) -> bool {
    matches!(info, INFO_FOUL | INFO_FOUL_UNDER_CHECK)
}

pub fn is_check_info(info: u8) -> bool {
    matches!(info, INFO_CHECK | INFO_CHECKMATE)
}

/// "b"/"w" を Color に変換する（protocol.rs の Sente/Gote と同じ意味）
pub fn parse_bw_color(s: &str) -> Option<Color> {
    match s {
        "b" => Some(Color::Sente),
        "w" => Some(Color::Gote),
        _ => None,
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct BotTurnRequest {
    /// 旧仕様（全履歴送信）では必須で "your_turn" が来ていた。差分化後の
    /// 型定義（README 2026-08 改訂）のトップレベルには載っていないため、
    /// 欠落時は "your_turn" とみなす
    #[serde(rename = "type", default = "default_request_kind")]
    pub kind: String,
    #[allow(dead_code)]
    pub request_id: String,
    pub game_id: String,
    /// "b" | "w"
    pub color: String,
    #[allow(dead_code)]
    pub number: u32,
    pub ply: u32,
    /// 差分化後の2回目以降のリクエストにのみ含まれる「botが保持済みと
    /// 想定される最終手数」。positions には basePly+1..=ply だけが入る
    #[serde(default)]
    pub base_ply: Option<u32>,
    /// 旧仕様のフィールド。差分化後の型定義には載っていないため任意にする
    #[allow(dead_code)]
    #[serde(default)]
    pub deadline_ms: u64,
    pub positions: HashMap<String, PositionEntry>,
    /// 初回リクエストにのみ含まれる（差分化後の2回目以降は省略される）
    #[serde(default)]
    pub game: Option<GameInfo>,
}

fn default_request_kind() -> String {
    "your_turn".to_string()
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct PositionEntry {
    #[allow(dead_code)]
    pub sfen: String,
    #[serde(default)]
    pub fouls: Option<FoulsField>,
    #[serde(default)]
    pub last_move: Option<String>,
    #[serde(default)]
    pub last_info: Option<u8>,
    #[serde(default)]
    pub last_capture: Option<String>,
    #[serde(default)]
    pub was_promotion: Option<bool>,
}

#[derive(Debug, Clone, Copy, Deserialize)]
pub struct FoulsField {
    pub b: u32,
    pub w: u32,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct GameInfo {
    #[serde(rename = "type")]
    pub kind: String,
    pub required_players: RequiredPlayers,
    #[serde(default)]
    pub param: Option<String>,
}

#[derive(Debug, Clone, Copy, Deserialize)]
pub struct RequiredPlayers {
    pub b: u32,
    pub w: u32,
}

/// 終局通知の `type`
pub const GAME_END_KIND: &str = "game_end";

/// 終局通知（`type: "game_end"`）。各Bot・各対局につき最大1回、再送なし。
/// 応答は本文なしの HTTP 204。`param` は Bot 一覧の棋譜ダウンロードと同じ形式で、
/// **相手の手も伏せられていない真の棋譜**（`moves=` / `move_durations=` /
/// `black_player=` / `white_player=` 等）が入る
#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct GameEndNotice {
    #[serde(rename = "type")]
    pub kind: String,
    #[serde(default)]
    pub bot_id: Option<String>,
    pub game_id: String,
    #[serde(default)]
    pub param: String,
    /// Checkmate / LossByFoul / Draw / Resign / TimeUp / WinByTry
    #[serde(default)]
    pub result: Option<String>,
    /// "b" | "w"、引き分けなら null
    #[serde(default)]
    pub winner: Option<String>,
}

/// payload の `type` を見て終局通知かどうかを判定する（手番の通知は `type` が
/// 省略されることがあるので、`game_end` と明示されたものだけを終局とみなす）
pub fn is_game_end(raw: &serde_json::Value) -> bool {
    raw.get("type").and_then(|t| t.as_str()) == Some(GAME_END_KIND)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_sample_first_move_payload() {
        let json = r#"{
            "type": "your_turn",
            "requestId": "r1",
            "gameId": "g1",
            "color": "b",
            "number": 0,
            "ply": 0,
            "deadlineMs": 1000,
            "game": {
                "type": "ついたて",
                "gameKind": 1,
                "promotionRank": 3,
                "drawMoveCount": 150,
                "requiredPlayers": { "b": 1, "w": 1 }
            },
            "positions": {
                "0": {
                    "sfen": "lnsgkgsnl/1r5b1/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL b - 1",
                    "fouls": { "b": 9, "w": 9 },
                    "times": { "b": 300, "w": 300 },
                    "byoyomiActive": { "b": false, "w": false }
                }
            }
        }"#;
        let req: BotTurnRequest = serde_json::from_str(json).unwrap();
        assert_eq!(req.kind, "your_turn");
        assert_eq!(req.ply, 0);
        assert!(req.base_ply.is_none());
        let game = req.game.as_ref().unwrap();
        assert_eq!(game.kind, "ついたて");
        assert_eq!(game.required_players.b, 1);
        assert_eq!(parse_bw_color(&req.color), Some(Color::Sente));
        let p0 = &req.positions["0"];
        assert_eq!(p0.fouls.unwrap().b, 9);
        assert!(p0.last_move.is_none());
    }

    /// 差分化後の2回目以降のリクエスト: `type`/`deadlineMs`/`game` が無く、
    /// `basePly` と差分の positions だけが来る
    #[test]
    fn parses_subsequent_diff_payload() {
        let json = r#"{
            "requestId": "r2",
            "gameId": "g1",
            "color": "b",
            "number": 1,
            "ply": 2,
            "basePly": 0,
            "positions": {
                "1": { "sfen": "masked", "lastMove": "+7776FU", "lastInfo": 0 },
                "2": { "sfen": "masked", "lastMove": "-0000ZZ", "lastInfo": 0 }
            }
        }"#;
        let req: BotTurnRequest = serde_json::from_str(json).unwrap();
        assert_eq!(req.kind, "your_turn");
        assert_eq!(req.ply, 2);
        assert_eq!(req.base_ply, Some(0));
        assert!(req.game.is_none());
        assert!(!req.positions.contains_key("0"));
        assert_eq!(req.positions["2"].last_move.as_deref(), Some("-0000ZZ"));
    }

    #[test]
    fn parses_masked_opponent_capture() {
        let json = r#"{
            "sfen": "rbsgk/4R/5/P4/KGSB1 w P 2",
            "fouls": { "b": 9, "w": 9 },
            "lastMove": "+1512HI",
            "lastCapture": "P",
            "lastInfo": 3
        }"#;
        let entry: PositionEntry = serde_json::from_str(json).unwrap();
        assert_eq!(entry.last_move.as_deref(), Some("+1512HI"));
        assert_eq!(entry.last_capture.as_deref(), Some("P"));
        assert_eq!(entry.last_info, Some(INFO_CHECK));
    }

    /// 仕様書の例そのまま
    #[test]
    fn parses_game_end_notice() {
        let json = r#"{
            "type": "game_end",
            "botId": "7c1d0e5b9a2f4c3e8d6b1a90",
            "gameId": "4f31a682d9c04b55b51aa231",
            "param": "&visibility_rule=tsuitate&black_player=Alice&white_player=Bot&moves=+7776FU-0000ZA&move_durations=2.00-1.00",
            "result": "Resign",
            "winner": "b"
        }"#;
        let raw: serde_json::Value = serde_json::from_str(json).unwrap();
        assert!(is_game_end(&raw));
        let end: GameEndNotice = serde_json::from_value(raw).unwrap();
        assert_eq!(end.game_id, "4f31a682d9c04b55b51aa231");
        assert_eq!(end.result.as_deref(), Some("Resign"));
        assert_eq!(end.winner.as_deref(), Some("b"));
        assert!(end.param.contains("moves=+7776FU"));
    }

    #[test]
    fn game_end_winner_null_is_draw() {
        let json = r#"{"type":"game_end","gameId":"g","param":"","result":"Draw","winner":null}"#;
        let end: GameEndNotice = serde_json::from_str(json).unwrap();
        assert!(end.winner.is_none());
    }

    #[test]
    fn your_turn_is_not_game_end() {
        let with_type: serde_json::Value = serde_json::json!({"type": "your_turn"});
        let without_type: serde_json::Value = serde_json::json!({"gameId": "g"});
        assert!(!is_game_end(&with_type));
        assert!(!is_game_end(&without_type));
    }
}
