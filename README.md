# Copilot Self-Learning Platform

> **レビュー修正版 v0.3（PR #4でmain反映済み）**: 誤採用を修正し、外部送受信は安全レビュー完了まで一時停止しています。従来の `-Upload` / `--execute` / `--use-bedrock` は実通信しません。以下の旧オンライン手順は現在利用できません。合成データのローカル処理・Dry Runは継続できます。社内IDによる承認、DLP、S3送信経路へのスナップショット統合は未完了です。[対応状況](docs/review-response-20261114.md) を先に確認してください。

GitHub Copilotで行った開発の**セッション情報を後から安全にS3へ蓄積**し、AgentCore MemoryとAgentRun型の学習を比較するためのPoCです。

**実装済み: ローカル一覧・選別・検査・ZIP化 / AWS CLIによる明示的なS3送信。** AgentCore学習・Hooks・自動Skill反映は未実装です。既存Copilotの動作に変更は加えません。

> **重要**: リポジトリは公開です。セッション履歴には業務の機密情報・ソースコード・個人情報が含まれる可能性があります。**S3送信・Gitコミット前に会社のルールに従って内容を確認し、送信許可を取得してください。** 自動検査は既知のトークン等だけを対象とし、情報漏えいを防ぐ保証はありません。

## 前提

- Windows + PowerShell 5.1以降（WSL2・Docker不要）
- Python 3.10以降（標準ライブラリのみ）
- S3に送信する場合のみ AWS CLI v2 と社内承認済みAWS権限・S3バケット・KMS鍵
- Copilot CLI / Copilot appで`~/.copilot/session-state/<session-id>/events.jsonl`があること。VS CodeのChat Export形式には未対応

## 1. ローカルセッションの一覧

PowerShellでこのリポジトリのルートから実行します。

```powershell
python .\scripts\prepare_sessions.py --list
```

保存先が異なる場合は `--source "C:\path\to\session-state"` を指定できます。

## 2. 終了済みのセッションをローカルに梱包

最初は**個別のセッションを1件選ぶ**ことをおすすめします。ZIPはGit管理外のフォルダへ出力します。

```powershell
python .\scripts\prepare_sessions.py `
  --session "<--listで確認したセッションID>" `
  --out "$env:USERPROFILE\copilot-learning-staging"
```

すべての終了済みセッションを対象にしたい場合は、`--session` の代わりに `--all` を指定します。最終更新から30分以内のセッションは既定で保留します。各ZIPには `events.jsonl`、任意の `workspace.yaml` と `plan.md` を入れ、別にSHA-256付き `*.manifest.json` を作成します。`checkpoints/` や `files/` は初期版では収集しません。

**出力したZIPを手元で展開し、機密・個人情報が含まれないか目視確認してください。** 判定が難しいデータはS3に送らないでください。

## 3. S3アップロード（既定はDry Run）

社内で承認済みのAWSアカウント・バケット・KMS鍵に置き換えて実行してください。`-Upload` を指定しない限りAWS通信は行いません。

```powershell
.\scripts\upload_sessions.ps1 `
  -StagingDirectory "$env:USERPROFILE\copilot-learning-staging" `
  -Bucket "<承認済みバケット>" `
  -ExpectedAwsAccountId "<12桁アカウントID>" `
  -Region "ap-northeast-1" `
  -KmsKeyId "<承認済みKMSキーARN>" `
  -Profile "<社内承認済みAWSプロファイル>"
```

Dry Runの内容、ZIP本体、社内のアップロード可否を確認した**後だけ**、最後に `-Upload -ConfirmApprovedData` を付けて実行します。STSでAWSアカウントIDを検証し、SSE-KMSでS3へ保存します。S3の同一キーへの上書きは禁止します。

```powershell
# 事前確認・社内承認済みの場合に限り、上記のコマンド末尾へ追加
-Upload -ConfirmApprovedData
```

**AWS上での実動作は未検証**です。初回は合成データ1件だけで動作確認してください。

## 4. ローカルテスト

```powershell
python -m unittest discover -s tests -v
```

CI / GitHub Actionsは使いません。

## 今後の拡張

S3に同じ入力を保存した後、AgentCore Memory標準（A）、AgentRun型カスタム（B）、ハイブリッド（C）を別々に動かして比較します。人間のフィードバックは `examples/feedback.example.json` をベースに `session_fingerprint` と紐づけます。

詳細な構成と受入条件は [docs/architecture.md](docs/architecture.md) を参照してください。

## Phase 1：AWSもCopilotも使わないオフライン実装（Draft）

**実装済み**: SHA・ZIPの再検証、イベントの安全側の正規化、AgentCore Memory `CreateEvent`投入計画の生成、AgentRun型の候補抽出・独立Graderのアダプター、Memory結果のハイブリッド判定、フィードバックのローカル保存・S3送信アダプター、合成データでの自動テスト。

**未検証/未実装**: Copilot実機の`events.jsonl`詳細形式への対応、AWS CLI・IAM・KMSの実接続、Episodic Reflectionの完了待機や品質評価、AgentCore Runtimeへのデプロイ、実タスクの成功率改善、CopilotへのSkills自動反映。**学習品質が実測で改善したとはまだ言えません。**

### 1. 合成データでの完全オフラインテスト

```powershell
python -m unittest discover -s tests -v
python .\scripts\demo_offline.py --out "$env:USERPROFILE\copilot-learning-demo-01"
```

`results/` に `normalized_session.json`、`memory_create_event_plan.json`、`agentrun_candidate_grades.json`、`comparison_report.json` ができます。Aは `not_executed` と表示されます。デモのB候補は**手作りの合成候補**であり、LLMが学習して作成した結果ではありません。

### 2. 実Copilotセッションのオフライン準備

```powershell
python .\scripts\prepare_sessions.py --list
python .\scripts\prepare_sessions.py --session "<ID>" --out "$env:USERPROFILE\copilot-learning-staging"
```

`session-<SHA256>.zip` と `session-<SHA256>.manifest.json` を確認し、内容を人手でレビューしてください。`--all` での一括収集は、初回では避けてください。

### 3. 学習処理のオフライン計画（外部通信なし）

```powershell
python .\scripts\run_learning_poc.py `
  --staging "$env:USERPROFILE\copilot-learning-staging" `
  --fingerprint "<64桁のSHA256>" `
  --out "$env:USERPROFILE\copilot-learning-analysis-01" `
  --memory-id "REPLACE_WITH_REAL_MEMORY_ID"
```

正規化は **`type=message` / `type=conversation_message` かつ `role=user/assistant/tool` で、本文が文字列のイベントに限定**しています。GitHubは`events.jsonl`の存在を公開していますが、ここで用いる細かなスキーマは実機未検証です。認識できないイベントは破棄・集計し、推測で本文を作りません。0件ならAgentCoreへの投入計画は空になります。

候補JSONを指定したオフラインGrade検証には `--candidates <JSON>` を使用します。AWS接続後は `--use-bedrock --model-id ... --expected-account-id ... --approval-file ... --confirm-approved-data` による実モデル生成を利用できます。モデル生成は AWS Bedrock Converse への外部送信です。承認なしでは実行できません。

### 4. 目視レビューの結果を登録し、S3へ送る（承認後だけ）

ローカル梱包時に本人が社内規程・送信先を照合します。承認証跡は指定したZIP・manifestのハッシュ、AWSアカウント、S3バケットに紐づきます。

```powershell
python .\scripts\review_session.py `
  --staging "$env:USERPROFILE\copilot-learning-staging" `
  --fingerprint "<SHA256>" --reviewer "<担当者識別子>" `
  --classification approved_non_sensitive --account-id "<12桁ID>" `
  --bucket "<承認済みバケット>" --approve --confirm-internal-policy
```

`upload_sessions.ps1` は新たに `*.approval.json` の存在と対象ハッシュ・送信先を検査します。実送信は `-Upload -ConfirmApprovedData` が必要です。**これらの確認フラグは社内のデータ取扱い承認そのものを保証しません。**

フィードバック登録は次のとおりです。実際に成功を確認したときだけ `verified_success` を使ってください。

```powershell
python .\scripts\add_feedback.py --staging "$env:USERPROFILE\copilot-learning-staging" `
  --fingerprint "<SHA256>" --out "$env:USERPROFILE\copilot-learning-feedback" `
  --outcome verified_success --lesson "環境を確認してからテスト" `
  --evidence "テスト12件PASS" --classification approved_non_sensitive
```

S3へ保存する場合は `scripts/upload_feedback.py` でレビュー済みのJSONを指定します（既定はDry Run、実送信は `--execute --confirm-feedback-reviewed` が必要）。

### 5. AWS環境を用意した後（コードは準備済み、実接続は未検証）

`run_learning_poc.py` が生成した `memory_create_event_plan.json` から、実在のMemory IDに対し `scripts/agentcore_memory_cli.py send` を明示的に実行します。STSでAWSアカウント照合後、冪等化トークン付き `CreateEvent` を順次投入。Episodicの反映は**非同期**です。

生成完了後、Memoryのnamespaceを指定して `scripts/agentcore_memory_cli.py export` でレコードを取得し、`run_learning_poc.py --memory-records <JSON>` でハイブリッド評価します。Memoryレコードに元イベントの根拠IDがない場合、採用せず `pending` として記録します。

### セキュリティと限界

- 本リポジトリは**公開**。ZIP、manifest、承認記録、Memory投入計画、フィードバック本体、候補の詳細、AWS資格情報をコミットしないでください。
- スキャンは既知の秘密パターンだけです。社内ソースコードや個人情報の流出防止は保証しません。社内の承認済み情報に限定してください。
- `keep` は**品質ゲートの判定**であり、Copilotに自動適用したことも、性能改善が実証できたことも意味しません。
- AWS CLI / Bedrock用の権限・リソース作成・料金・Windows PowerShell動作は未検証です。AWSやGitHub ActionsでのCIは構成しません。

詳細は [docs/offline-pipeline.md](docs/offline-pipeline.md) を参照。


## AgentRunとの詳細比較（2026-11-15）

[処理フロー・関数対応・未実装機能・評価計画](docs/agentrun-process-comparison-20261115.md) を参照してください。
