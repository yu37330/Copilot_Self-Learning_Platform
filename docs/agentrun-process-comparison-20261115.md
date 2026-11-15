> **対象に関する補足（2026-11-15）**：このプロジェクトはWindows PCでGitHub Copilotを使う個人・チーム・企業の開発者を想定します。以下の『会社』『社内』は当時検討していた管理環境の履歴上の表現で、企業専用を意味しません。AWSの外部通信停止は利用形態にかかわらず維持します。

> **履歴資料としての注意（2026-11-15追記）**：以下は2026-11-14時点のPoC v0.3との比較です。本文の「知識台帳・検索・S3 Version ID・自動起動が未実装」等は**当時の記録**で、現在の機能一覧ではありません。現在は知識台帳・検索、単一CodeZip Runtime、S3 ready→Lambda、Memory待ち再開、OKF Wikiを実装しています（実AWSは未検証）。現在の正本は[現状](current-status-20261115.md)と[最新構成](architecture.md)。AgentRun本体を導入したわけではありません。

# AgentRun と Copilot Self-Learning Platform の実装・処理フロー比較

更新: 2026-11-15  
比較基準: AgentRun [a77faccc](https://github.com/Parcha-ai/agentrun/tree/a77faccc7911eb0c1c61020645ace45b80f8121d)（外部レビューで使用した固定リビジョン） / Copilot Self-Learning Platform [PR #4 マージ](https://github.com/yu37330/Copilot_Self-Learning_Platform/pull/4)。  
**注意**: 実装有無・設計の比較であり、両者を同じ実タスクで走らせた性能比較ではない。AgentCore Memory/Bedrock、実Copilot、Windows PowerShell、AWSの実機動作はいずれも未検証。

## 比較の結論

- **AgentRun**: タスク実行 → 実行後Reflection → 提案の構造/機密チェック → 一般化の事前判定 → LLMによる採否 → Gitへの整合性付きMerge → 次回Runで知識・Skill・Workflowを再利用、という **運用中のエージェントに組み込まれた閉ループ**。
- **本PoC v0.3**: 既存Copilotの終了済みセッション → 選別/ZIP化/レビュー → S3保存を想定（安全上、実送信停止中）→ オフライン正規化 → Memory投入計画、AgentRun型候補と独立Grader、Hybrid評価の下地、という **後追い分析・比較評価向け基盤**。
- **最大の差**: 現行PoCには「承認済み知識の確定台帳」「次タスクに適した知識の検索」「Copilotへの適用」「実行後の再評価」「Workflowとしての実行」がない。したがって **学習閉ループがまだ閉じていない**。

## AgentRun の処理フロー（固定リビジョン）

```mermaid
flowchart TD
  A[エージェント定義 learning 有効] --> B[学習Git mainの確定コミットをpin]
  B --> C[BM25で関連Note選択 + Skill/Workflow供給]
  C --> D[エージェント実タスク実行]
  D --> E{終端・予算・新規知見の条件}
  E -->|学習候補あり| F[ツールなしでReflection]
  E -->|学習不要| Z[今回は保存なし]
  F --> G[Markdown Note/Skill/Workflow草案を解析]
  G --> H[構造/サイズ/秘密/危険な参照/Workflow Dry Run]
  H --> I[Noteの一般化事前判定 Jev 任意]
  I --> J[独立Grader keep/pending/drop]
  J --> K[Git mainにCASとRun-IDでマージ]
  K --> L[承認・active化・supersede/retire]
  L --> B
  C --> M{一致するWorkflowが1つあるか}
  M -->|ある| N[Workflow-firstでDSL実行]
  N -->|エスカレーション・失敗| D
  ```

公式実装: [reflect.ts](https://github.com/Parcha-ai/agentrun/blob/a77faccc7911eb0c1c61020645ace45b80f8121d/packages/serve/src/learn/reflect.ts)、[check.ts](https://github.com/Parcha-ai/agentrun/blob/a77faccc7911eb0c1c61020645ace45b80f8121d/packages/serve/src/learn/check.ts)、[general.ts](https://github.com/Parcha-ai/agentrun/blob/a77faccc7911eb0c1c61020645ace45b80f8121d/packages/serve/src/learn/general.ts)、[grade.ts](https://github.com/Parcha-ai/agentrun/blob/a77faccc7911eb0c1c61020645ace45b80f8121d/packages/serve/src/learn/grade.ts)、[commit.ts](https://github.com/Parcha-ai/agentrun/blob/a77faccc7911eb0c1c61020645ace45b80f8121d/packages/serve/src/learn/commit.ts)、[workflow-first.ts](https://github.com/Parcha-ai/agentrun/blob/a77faccc7911eb0c1c61020645ace45b80f8121d/packages/serve/src/runner/workflow-first.ts)。

## 本PoC v0.3の処理フロー

```mermaid
flowchart TD
  A[Copilotで作業] --> B[終了済みsession-stateを手動選択]
  B --> C[prepare_sessions: secret検査・ZIP・manifest・SHA]
  C --> D[人手レビュー・自己申告の承認記録]
  D --> E[upload_sessions: 現在は実送信停止]
  C --> F[read_archive: ZIP/manifest/fingerprint再検証]
  F --> G[normalize_events: 許可したmessageのみ変換]
  G --> H[build_memory_requests: CreateEvent計画JSON]
  G --> I[候補JSONまたはBedrockで候補生成: 現在オンライン停止]
  I --> J[grade_candidates: 根拠ID・候補単位feedback・禁止句・重複]
  J --> K[independent_llm_grade: モデル呼出しは現在停止]
  H -. AWS利用可能後 .-> L[AgentCore Memory: Episodic/Reflection]
  L -. 後日 .-> M[Memory recordsを候補変換・Hybrid grade]
  K --> N[ローカルJSON: 比較レポート]
  M --> N
  N --> O[知識確定台帳 / Copilotへの検索・再利用は未実装]
  ```

実装: [prepare_sessions.py](../scripts/prepare_sessions.py)、[core.py](../copilot_learning/core.py)、[learning.py](../copilot_learning/learning.py)、[review_safety.py](../copilot_learning/review_safety.py)、[run_learning_poc.py](../scripts/run_learning_poc.py)。

## ステージごとの詳細比較

| 観点 | AgentRun（固定リビジョン） | 本PoC v0.3 | 本PoCの状態 |
|---|---|---|---|
| 起動と収集 | エージェントの各Runを自前Harness/Runnerが把握。Run終了時に学習判定 | Copilot外側にあり、終了後の `events.jsonl` を手動収集 | セッション収集コードあり、実ログ形式未確認 |
| 学習時機 | Run終了後の振り返りターン。予算上限、失敗、再開などを考慮 | ZIPを後から任意時点でバッチ処理 | バッチ実装あり、自動トリガなし |
| 学習開始時の既存知識 | Git mainのコミットを固定してseed。関連NoteのBM25選択、Skill/Workflowを供給 | 既存Knowledgeは入力として自動選択しない | **未実装** |
| 入力正規化 | Harnessの実行記録・ユーザー発言・工具利用をホストが管理 | `type=message/conversation_message` など許可リストのみ。未知イベントは欠落理由を記録 | 合成スキーマのみ実装 |
| Reflection | 同一セッションへツールなしの追加ターンを要求。不要なら何も出さない | `extraction_prompt` + Bedrock Converseで候補作成のコード。現在は送信停止 | オフライン候補入力だけ検証 |
| 学習成果物 | Note最大8件、Skill最大1件、Workflow草案最大1件／Run | 同じ上限で候補JSONを検査 | 候補の種類はあるが実Skillは生成・配布していない |
| 構造安全性 | ファイル名・パス・サイズ・危険な変更・秘密情報・Workflow DSL/Dry Runを検査 | スキーマ、既知Secret正規表現、根拠ID、危険指示パターン | 基本実装。DLPではない |
| 一般化の判定 | Noteが元の入力以外に通用するかJevで任意の事前判定（標準閾値0.5）。未実行なら後段Grader | `generalization`文言は保持するが独立の確率判定・適用除外テストなし | **不足** |
| 独立評価 | 既存のactive Notes、常設Instructions、ユーザー発言、重複・矛盾・危険性を見てkeep/pending/drop | 候補ごとのevidence/成功・失敗feedbackを照合。独立LLM Graderは格下げのみ | **レビュー指摘の誤採用を修正**。実効果未実証 |
| 永続化 | Gitの1コミットでマージ。競合時はCAS再試行。Run-IDで重複防止 | ZIP＋manifestとJSON出力。S3保存は設計済みだが実通信停止。採用知識の正式台帳なし | 部分実装 |
| 知識の履歴と状態 | active/pending/retired/superseded、承認・復元・差分ログ | `keep/pending/drop` をJSONに出力。長期の承認・更新・supersede処理なし | **未実装** |
| 検索と再利用 | BM25（英数字単語）、名前・説明・本文。変更ファイルpathは3倍の重み。Skillも次Runへ渡す | 検索サービスもCopilotへの注入も実装していない | **未実装** |
| Workflow自動化 | JSON DSLを検証・Dry Run。構造的に単一適合するとworkflow-first、判定不能等は同じRunのエージェントへ引継ぎ | workflowという候補分類は可能。ただし型付きDSLや実行器がない | **未実装** |
| Workflowの経済性 | 実行費用とエージェント通常費用を比較し、高コストの場合は要改訂とする仕組みあり | AWSコスト/トークンを保存する下地だけ。比較未実施 | **未実装** |
| 評価（Evals） | Held-out評価値を登録・表示可能。正解率・95% Wilson区間、費用。評価ケースの実行は別途 | `comparison_report.json` は候補数と判定数中心。A/B/C/Dの共通ブラインド評価なし | **未実装** |
| 再現性 | Git seed、Run-ID、学習差分、スナップショット | ZIP SHA、manifest SHA、candidate/feedbackハッシュ。S3 Version IDは未記録 | 部分実装 |
| 承認と外部送信 | 危険な学習をpendingにし利用しない。ホストがツール権限を制御 | 人手レビュー記録は社内ID署名の裏付けなし。外部I/Oは安全停止 | **組織導入未承認** |

**注意**: AgentRun側にも安全上の境界・プロンプト/モデル依存があり、構造検査とLLMのGraderだけで知識の真実性は保証されない。上記は処理機能の比較であって品質の優劣実測ではない。

## ロジックの対応箇所

| AgentRunコード | 本PoCコード | 差 |
|---|---|---|
| `learn/reflect.ts: reflectPrompt` | `learning.py: extraction_prompt` | AgentRunはRun終端の同一セッション振り返り。PoCは後追いテキスト入力 |
| `learn/check.ts: checkProposal` | `learning.py: validate_candidates`, `grade_candidates` | AgentRunはファイルやDSLまで構造検査。PoCはJSON候補中心 |
| `learn/general.ts: generalScores` | 該当なし | 元入力以外への汎用性の事前チェック |
| `learn/grade.ts: gradePrompt` | `learning.py: independent_llm_grade` | AgentRunは既存Noteとユーザー発言も比較。PoCは現セッションの証拠・反証中心 |
| `learn/commit.ts: learningCommit` | `run_learning_poc.py: _safe_write` | AgentRunは版管理と採用状態を更新。PoCは結果JSONを固定出力 |
| `learn/relevance.ts: bm25/queryOf` | 該当なし | 知識の関連検索・予算内の再利用が未実装 |
| `runner/learning-seed.ts: layLearningSeed` | 該当なし | 次RunへNote/Skillsを渡す仕組みが未実装 |
| `runner/workflow-first.ts` | 該当なし | DSL実行・エスカレーション・コスト監視なし |
| `learning-evals.ts` | `learning.py: make_report` | Held-out正解率/区間/費用の集計は未実装 |

## 重要な設計差（安易な移植を避ける）

1. **原本の位置**: AgentRunのGitは『採用済み知識』の正本。本PoCのS3は『学習前のセッション原本』の正本。用途が異なるのでS3をGitで置き換えるのではなく、**S3原本＋版付き知識台帳**に分離する。
2. **確証**: v0.2は別作業の成功を借りて誤候補をkeepする問題があった。v0.3では`candidate_sha256`・`evidence_ids`・対象feedbackを照合し、反証/欠落があればpendingにする。ただし**適用すべき場面・適用してはいけない場面**の判定はまだない。
3. **日本語検索**: 固定AgentRun実装`relevance.ts`の語抽出は `/[a-z0-9]+/g`。日本語Notes向けには文字n-gram、形態素解析、ハイブリッド検索を試し、実タスクの適用精度で比較すべき。
4. **AgentCore Memory**: Episodic Reflectionを生成できても、元イベントから導かれる根拠IDの対応・採用基準・Copilotへの配布は自動では成立しない。Memoryは知識抽出・検索の候補として扱い、別の評価・承認層を設ける。
5. **安全停止**: v0.3では`-Upload`/`--execute`/`--use-bedrock`を用いた実通信を停止中。会社の正式承認・PII/DLP・送信内容固定が完成するまで解除しない。

## 比較を成立させるための推奨実装順序

| 優先 | 追加実装 | 完了条件 |
|---|---|---|
| P0 | S3原本ID・確定SHAと送信/抽出計画の結び付け | 入力差し替え・承認外送信がモック呼び出し0で拒否 |
| P0 | feedbackと候補の適用条件・反例のスキーマ | 関係ない成功からkeepしない。境界例をpendingにする |
| P1 | 有用知識の版管理とライフサイクル | `proposed/pending/active/retired/superseded` と根拠が追跡可能 |
| P1 | 関連知識検索とCopilotへの**読み取り専用**注入 | 既知課題と適用除外課題で適用/非適用を区別できる |
| P1 | A Memory標準、B 独自、C Hybridの共通抽出/採点 | 同一データ・モデル予算・保存形式で比較。未実行はnull |
| P1 | Held-out評価と誤適用計測 | 適用精度、誤採用率、正解率、再発率、トークン・円/成功を計測 |
| P2 | Skill→Workflow草案→DSL実行/フォールバック | 合成Dry Run、権限、停止、二重実行、費用評価を検証 |
| P2 | 社内承認・DLP・実環境統合 | Windows/AWS/AgentCoreの実機と承認証跡を独立検収 |

### 検証用タスクの作り方

- **適用例**: 前回、仮想環境の不一致で失敗したPythonテストと同類だが、別のプロジェクト・別の入力で、環境確認手順が有効かを見る。
- **適用除外例**: Pythonの問題ではなく、APIレスポンスの仕様不一致でテストが失敗しているケース。仮想環境確認のSkillを無条件適用すると失敗判定にする。
- **反証例**: セッションの一部に成功があっても、引用しているビルド結果が失敗していれば「ビルド成功」を採用させない。
- **比較条件**: 訓練・評価をプロジェクト/問題系列で分離。A/B/C/Dで同じ未学習課題、モデル、予算、採点器を使い、知識なしDも評価する。

## 確認した一次資料

- AgentRun固定コード: [reflect](https://github.com/Parcha-ai/agentrun/blob/a77faccc7911eb0c1c61020645ace45b80f8121d/packages/serve/src/learn/reflect.ts), [check](https://github.com/Parcha-ai/agentrun/blob/a77faccc7911eb0c1c61020645ace45b80f8121d/packages/serve/src/learn/check.ts), [general](https://github.com/Parcha-ai/agentrun/blob/a77faccc7911eb0c1c61020645ace45b80f8121d/packages/serve/src/learn/general.ts), [grade](https://github.com/Parcha-ai/agentrun/blob/a77faccc7911eb0c1c61020645ace45b80f8121d/packages/serve/src/learn/grade.ts), [commit](https://github.com/Parcha-ai/agentrun/blob/a77faccc7911eb0c1c61020645ace45b80f8121d/packages/serve/src/learn/commit.ts), [relevance](https://github.com/Parcha-ai/agentrun/blob/a77faccc7911eb0c1c61020645ace45b80f8121d/packages/serve/src/learn/relevance.ts)
- AgentRun公式ドキュメント: [学習フロー](https://agentrun.ai/docs/self-improvement), [学習仕様](https://agentrun.ai/docs/learning-reference), [Workflow DSL](https://agentrun.ai/docs/guide)
- 本PoC: [README](../README.md), [オフライン仕様](offline-pipeline.md), [レビュー修正一覧](review-response-20261114.md)
