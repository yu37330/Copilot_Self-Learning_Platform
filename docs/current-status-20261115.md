# 現在の実装状況・残課題（2026-11-15）

**対象はWindows PCでGitHub Copilotを使う開発者（個人・チーム・企業）**です。**基準main：PR #27 / commit `5b47aa9`。** 「実装済み」はコードと合成データでの検証を意味し、**AWS実デプロイ・実LLM処理・利用形態に応じた安全性/認可検証は未完了**です。

| 対象 | 実装済みの範囲 | 未完了 |
|---|---|---|
| Copilot入力 | ローカル選別/ZIP・manifest・SHA・機密パターン検査 | Windows Copilotの実イベント形式と必要な情報保護 |
| 独自学習 | Note/Skill/Workflow候補、根拠・反証、独立Grade | 実LLMの学習効果、Workflow実行 |
| Memory | CreateEvent/ページング取得、待機再確認 | 実AWS抽出、完全終了の独立証明 |
| Wiki | WM/WOからのBedrockモデル呼出経路・引用/OKF v0.2出力 | 実モデル・意味的正確性・正式承認 |
| Runtime | **1 CodeZip Runtime**、S3→Lambda、DynamoDBジョブ台帳、6モード | AWS ARM64起動・CFN配備 |
| 自動再開 | Scheduler→同じLambda、有限回/条件付き、既定OFF | GSI/Schedulerの実配送 |
| 比較実験 | 13学習×3Wiki＝**39条件の未実行計画** | 全条件runnerと実タスク/費用評価 |
| Copilot再利用 | SQLite台帳・日本語検索・Skill preview | 自動配布/実使用結果の自動収集 |

### 直近PRの検証実績

- [PR #26](https://github.com/yu37330/Copilot_Self-Learning_Platform/pull/26)：Runtime統合とCodeZip。**新規64＋関連既存131＝195件の対象テストPASS**。ローカルZIP起動`/ping=200`、外部停止`/invocations=503`。実AWS呼出0。
- [PR #27](https://github.com/yu37330/Copilot_Self-Learning_Platform/pull/27)：WAITING_MEMORYの有限回自動再開。**新規34＋関連既存64＝98件の対象テストPASS**。合成待機→再開→Wiki/OKFを確認。更新ZIP圧縮約15.98MB。実AWS呼出0。
- これらは**別々の対象範囲**の検証結果で、加算して全リポジトリの実測数とはしません。CI/GitHub Actionsは使用していません。

### 最優先の残課題

1. **安全**：[Issue #6](https://github.com/yu37330/Copilot_Self-Learning_Platform/issues/6)。データ所有者の同意、送信範囲・認証/情報保護の検証が必要（組織利用では追加のSSO/承認/DLP）。`require_online_release()`は引き続き常時拒否。
2. **AWS実環境**：[Issue #25](https://github.com/yu37330/Copilot_Self-Learning_Platform/issues/25)。CodeZip、ARM64/AL2023、CFN/IAM/KMS、S3通知、DynamoDB GSI、Schedulerの受入未実施。
3. **Memory完全性**：CreateEvent受領／安定した複数回読取はExtraction/Reflectionの終了証拠ではない。`memory_extraction_complete=false`／`complete_for_comparison=false`を維持。
4. **実験と再利用**：[Issue #10](https://github.com/yu37330/Copilot_Self-Learning_Platform/issues/10)／[Wiki #21](https://github.com/yu37330/Copilot_Self-Learning_Platform/issues/21)。39条件の共通runner、モデル・コスト・実タスク成績、OKFの意味検証、Copilot自動配布が未完成。

**次の実装提案**：39条件とRuntime6モードの実行/観測adapter、抽出完了の検収設計、費用の上限・ログ、実環境を必要としない追加テスト。AWS側の操作は対象AWSアカウントの利用権限とデータ送信許可を確認した後に受入検証します。S3通知・Schedulerは既定OFFのままです。

詳細は[全体図](architecture.md)、[オフライン処理](offline-pipeline.md)、[CodeZip手順](single-runtime-codezip-20261115.md)、[Memory待ち再開](memory-resume-20261115.md)、[39条件比較](full-experiment-suite-20261115.md)、[OKF Wiki](okf-wiki-20261115.md)。追跡の親Issueは[全体 #2](https://github.com/yu37330/Copilot_Self-Learning_Platform/issues/2)です。
