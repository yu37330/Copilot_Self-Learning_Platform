# Copilot Self-Learning Platform — アーキテクチャ v2

更新: **2026-11-15 / PR #27まで**。**対象はWindows PCでGitHub Copilotを使う開発者（個人・チーム・企業）**です。単一AgentCore Runtime（CodeZip）のコードと合成E2Eは実装済みですが、**AWSへは未デプロイ**。実Copilotの保存形式は対象PCで未検証。初期の「S3保存のみ」のPhase 0設計とは異なります。Managed Harnessは使用しません。

## 全体構成

```mermaid
flowchart TB
  A[Windows Copilot session-state] --> B[選別/ZIP/SHA/機密検査]
  B --> C{データ所有者の許可・送信先の確認}
  C -->|未承認| D[ローカル保留]
  C -. 承認後のみ .-> E[(S3 private/versioned: inputs/objects)]
  E --> F[inputs/ready/*.job.json: VersionId/SHAを固定]
  F --> G[Lambda: S3イベント起動]
  H[EventBridge Scheduler: 既定OFF] --> G
  G --> I[単一AgentCore Runtime: CodeZip]
  I --> J[Python Pipeline: 6モードとcheckpoint]
  J --> K[Amazon Bedrock: 推論]
  J --> L[AgentCore Memory: 外部サービス]
  I --> M[(DynamoDB: ジョブ状態・WAITING_MEMORY)]
  J --> N[(S3 results/job_id/: OKF・complete.json)]
```

**「Runtime 1つ」は実行環境が1つの意味**で、すべてのMemory/実験条件を混在させる意味ではありません。Lambdaはセッション本文ではなく固定ジョブ参照を送ります。HTTP 202は受付であって完了ではなく、S3の`complete.json`とDynamoDBの`SUCCEEDED`で確認します。

## 実コードとの対応

| 役割 | 実装先 | 状態 |
|---|---|---|
| セッションのローカル選別 | `scripts/prepare_sessions.py` | 合成入力で検査、実Copilot書式未確認 |
| Runtime HTTP入口 | `main.py` / `runtime_service/server.py` | `/ping`・`/invocations`・worker |
| 許可設定・S3/Memory/LLM | `runtime_service/contracts.py` / `aws_adapters.py` | VersionId/SHA/KMS/許可モデルを照合、実通信停止 |
| 6モード統合 | `runtime_service/pipeline.py` | 独自抽出、Memory、Wiki、OKF |
| 起動・有限回再開 | `lambda_function.py` / `runtime_service/trigger.py` / `resume.py` | 同じLambda／Runtime、Scheduler既定OFF |
| 知識抽出と安全判定 | `copilot_learning/learning.py` | Note/Skill候補・反証・Grade |
| Wiki/OKF | `copilot_learning/wiki.py` / `okf.py` | WM/WOの出典/脚注/版検査 |
| 比較計画 | `experiment_suite.py` / `wiki_matrix.py` | **13×3＝39条件の準備計画のみ** |

## 単一Runtimeの6モード

| mode | 実行経路 |
|---|---|
| `custom` | 原本→独自抽出→Grade |
| `original_wiki` | 原本→Bedrock用Wiki候補→引用検査→OKF |
| `custom_and_original_wiki` | 同じ原本から独自抽出と原本Wiki（Memoryなし） |
| `memory_ingest` | 原本→AgentCore Memory投入 |
| `memory_wiki` | Memory投入・取得→**暫定**Wiki→OKF |
| `hybrid` | Memory取得結果→独立Grade |

6モードのモック統合は、39条件の公平な実験を完了したことを意味しません。Memoryの`CreateEvent`受理や`ListMemoryRecords`の安定した複数回読取は**非同期抽出/Reflection完了の証拠ではありません**。

## 入出力・再開

```text
inputs/objects/              セッションZIP・manifest・任意feedback（VersionId/SHA）
inputs/ready/*.job.json      最後に置く起動ファイル
results/<job_id>/            checkpoints、暫定知識、OKF、complete.json
DynamoDB                   RUNNING / SUCCEEDED / FAILED / WAITING_MEMORY
                           上限・条件異常 → RECOVERY_REQUIRED
```

Memory待ちはGSIの候補を本体の強整合読取で再確認し、revision/期限を照合してScheduler→**同じLambda**→**同じRuntime**へ送ります。`EnableMemoryResume=false`とS3通知OFFが既定。`FAILED`や期限切れ`RUNNING`を無条件再実行しません。API成功直後・checkpoint前の強制停止までexactly-onceを保証しません。

## 安全性と残課題

`require_online_release()`はAWS実送受信を無条件停止します。フラグでは解除不可。公開Gitへ個人/組織の非公開セッション・承認履歴・鍵・機密辞書を保存しません。ARM64/AL2023実起動、CFN/IAM/KMS、Scheduler/GSI/S3通知の実動作、実LLM/Memory/Copilot、利用形態に応じた認証/情報保護（組織利用ではSSO/DLP等）は未検証です。Wikiは`draft`、`approved_for_copilot=false`で、39条件runner/費用実測/安全なCopilot自動適用も未完成です。

次に読む資料: [現在地](current-status-20261115.md)、[Runtime/CodeZip](single-runtime-codezip-20261115.md)、[Memory再開](memory-resume-20261115.md)、[比較実験](full-experiment-suite-20261115.md)、[Wiki/OKF](okf-wiki-20261115.md)。

## 追記（2026-11-19、Issue #31）：オフライン指示改善プレビュー

既存の単一CodeZip Runtimeに新たなネットワーク処理を追加せず、ローカルの保存・正規化処理を再利用する独立分析経路を用意した。

~~~text
prepare_sessions.py の検証済みZIP/manifest
    → core.read_archive / core.normalize_events
    → session_retro.analyze_sessions（既知の兆候7種類）
    → Git管理外の新規ディレクトリにpreview JSON/Markdown/manifest
    → 人手確認・verify_preview（Copilot設定は変更しない）
~~~

これは既存のSkillプレビュー/Knowledge Store/Runtime処理とは分離した**提案候補の検出**である。既存のセッション出典ID・archive SHAを保持し、原文はプレビューへ転載しない。Phase BでのLLMによる根本原因分析、AGENTS.md等の実差分生成、既存指示との矛盾検査、Phase Cでのheld-out比較は未実装である。

[使用方法・制限・合成テスト](session-retro-20261119.md)。


## 追記（2026-11-20、Issue #41）：第3系統 ChatGPT Task Improvement

Copilot専用の①知識学習系と②Session Retrospectiveに加えて、ChatGPTへの業務依頼を評価する③ChatGPT Task Improvementの**オフライン匿名集計経路**を分離した。

- 入力1：ユーザーが選んだChatGPT公式データエクスポート内の会話JSON（アクティブな会話分岐のuser/assistantだけ）
- 入力2：ユーザーが選んだDesktop Commanderのtool-history.jsonlまたはJSON配列
- 実行：analyze_chatgpt_desktop_offline.py → chatgpt_task_trace.evaluate_sources → 固定の改善候補と匿名集計 → manifest SHAによる再検証
- 保持しない情報：会話本文、タイトル、実行コマンド、コマンド引数、ツール出力、任意の内部ツール名、ファイルパス
- 安全：機密語辞書と明示的なローカル同意が必要。既存Git管理外の新規フォルダにだけプレビュー出力。AWS・LLM・Copilotへの送信/自動適用なし
- 紐付け：共通task IDがないため、時刻近接だけではChatGPT会話とツール履歴を結合しない。業務完了の独立証拠は未取得
- 検証：合成データでのE2Eのみ。実会話エクスポートと実Desktop履歴の解析は未実施

[ChatGPT Task Improvementの入力・出力・制約](chatgpt-task-improvement-20261120.md)。
