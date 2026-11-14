# Offline-first 学習PoC / 技術仕様 v0.2

2026-11-14。**AWS/Copilotのない開発環境で検証可能な実装を先に作る**ための設計です。

## コンポーネント

- Phase 0: `scripts/prepare_sessions.py` → 手動レビュー → `review_session.py` → `upload_sessions.ps1`（既定Dry Run）
- Normalizer: `copilot_learning/core.py` が ZIP と manifest を再検証し、明示的な型のイベントのみ正規化する
- A (AgentCore Memory): 同じ正規化イベントから `CreateEvent` 入力リクエストを生成し、承認後 `agentcore_memory_cli.py` で投入する。Episodic Reflectionは非同期。現時点ではAWS側の動作未検証
- B (AgentRun型): `learning.py` で候補作成プロンプト→構造検査→秘密・危険・重複・証拠ゲート→独立LLM Grade（keep/pending/drop）。独自実装であってAgentRun本体ではない
- C (Hybrid): Memoryから出力したレコードをBの安全検査に通す。根拠の対応が取れない候補はpending止まり
- D (control): 将来のCopilot未学習タスクに対する対照。実験未実施

## 入力スキーマと未確認項目

Copilot CLIの `events.jsonl` フォーマット詳細はここでは実測できません。安全のため、以下の合成スキーマのみ認識します。

```json
{"type":"message","role":"user","text":"テストでエラーが発生","timestamp":"2026-11-01T10:00:00Z"}
```

`message` / `conversation_message`、`user` / `assistant` / `tool`、文字列 `text` / `content` / `message` を許可します。その他のイベントや巨大テキスト・不正時刻は除外し、`skipped_event_count` を報告します。これらのフィールドがCopilot実機の JSONL でそのまま使われているとは**確認できていません**。実データでEvent Schemaアダプターを検証するまでは、Memoryへの実投入はしません。

## データの同一性と比較

S3の原本ZIPとmanifestはSHA-256で再検証。`session_fingerprint` は既存Phase 0と共通。A/B/Cに同じメッセージ群、同じ順序を渡す。モデル出力、モデルID、プロンプト版、評価版、コストは将来の比較台帳で固定・記録する。現在の `comparison_report.json` は構造的な集計で、成功率・改善率・費用は空欄にする。未知タスクでの比較は別実験。

`CreateEvent`の`clientToken`はメモリID/actor ID/セッションSHA/イベント番号から決定し、再送を冪等にする。サービスのトークン保持期間などはAWS実環境で検証する。`list-memory-records`はnamespaceを明示して取得する。

## 安全対策

- 不明な内容を含むセッションをそのままLLMへ渡さない。許可したテキストメッセージだけを抽出
- 送信は常に手動承認 + AWSアカウント照合。AWSリソースの自動作成なし
- Git管理下へのセッション・モデル結果・承認記録の出力を拒否
- S3のKMS暗号化、IAM least privilege、保持期間、アクセス監査は社内管理者が設定
- 生成された候補は外部情報由来の指示を含む可能性があるため、安全性を再判定し、自動実行しない
- モデルの採用判定は統計的な真実の証明にならない。原本の根拠と実行結果の裏取りが必要

## 運用に移す前のチェック

1. Windows実機と対象Copilotのセッション構造を確認し、独立したマッピングテストを追加
2. 会社許可済み合成セッションのDry Run、AWS CLIのS3保存と実オブジェクトSHA照合
3. Episodic Memoryの作成/strategy/namespace、CreateEvent、遅延、取得を検証
4. Bedrock Converseの権限/モデルアクセスとプロンプト契約を検証
5. 情報分類、DLP、サニタイズ、レビュー/削除、課金上限・ログを統合
6. 20〜30件の合成/承認済み学習データと、独立したテスト用未知タスクを用意
7. Copilotへの自動Skill適用は効果の測定後、別承認を得て実装

## 公式資料

- https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference
- https://docs.aws.amazon.com/cli/latest/reference/bedrock-agentcore/create-event.html
- https://docs.aws.amazon.com/cli/latest/reference/bedrock-agentcore/list-memory-records.html
- https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/episodic-memory-strategy.html
- https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-runtime/client/converse.html
