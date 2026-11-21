// ブラウザ単体（Tauri の外）で UI を確認するための invoke モック。
// `http://localhost:1421/?mock=1` で開いたときだけ有効になる。
// 対局モードは、途中まで進めた対人の局面1つと両方の席の入力プレーンだけを返す（指し手は進まない）。
// フィクスチャは src-tauri の ignored テストで生成する（public/fixtures/ は gitignore 済み）:
//   FIXTURE_DIR=$(pwd)/public/fixtures cargo test --manifest-path src-tauri/Cargo.toml -- --ignored fixtures

export async function installMockIfRequested(): Promise<void> {
  if (!new URLSearchParams(location.search).has("mock")) return;
  if ("__TAURI_INTERNALS__" in window) return;
  const fixture = async (name: string) => {
    const res = await fetch(`/fixtures/${name}.json`);
    if (!res.ok) throw new Error(`fixture ${name} がありません（生成手順は mock.ts 冒頭）`);
    return res.json();
  };
  (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {
    // イベントの購読（進捗の listen）が呼ぶ。モックではイベントは来ないので番号だけ返す
    transformCallback: () => 0,
    invoke: async (cmd: string, args?: Record<string, unknown>) => {
      if (cmd.startsWith("plugin:")) return null; // event listen / dialog open は何もしない
      switch (cmd) {
        case "engines":
          return ["estimator", "estimator_v10", "estimator_v9", "heuristic"];
        case "list_scenarios":
          return fixture("scenarios");
        case "load_kifu":
          return fixture("kakutori");
        case "eval_tally":
          return fixture("tally");
        case "eval_ranking":
          return fixture("ranking");
        case "eval_king_belief":
          return fixture("king_belief");
        case "clear_king_belief_cache":
          return;
        case "cancel_eval":
          return;
        // 対局モード: 途中まで進めた対人の局面を1つ返すだけ（指し手は進まない）
        case "play_start":
        case "play_view":
          return fixture("play_view");
        case "play_planes":
          return fixture(`play_planes_${args?.side === "gote" ? "gote" : "sente"}`);
        // 振り返り・リプレイの入力プレーン: どの手数でも同じ1局面ぶんを返す
        case "play_review":
          return "mock-review.kif";
        case "kifu_planes":
          return fixture("kifu_planes");
        default:
          throw new Error(`mock 未対応のコマンド: ${cmd}`);
      }
    },
  };
  console.log("[mock] Tauri invoke をフィクスチャで置き換えました");
}
