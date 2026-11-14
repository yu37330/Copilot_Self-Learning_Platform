# Copilot Self-Learning Platform

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
