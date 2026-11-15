# Offline-first 学習パイプライン — 現行仕様

更新: **2026-11-15 / PR #27まで**。初期v0.2のS3保存PoCに加え、単一CodeZip Runtimeの入口、6モード、Memory待ち再開の**コード・モック統合**が実装されています。実AWS/Copilot/LLMはまだ実行していません。

```text
Copilotセッション → prepare_sessions.py → ローカルZIP・SHA・機密検査
   → [社内承認が必要・現在はAWS実送信停止]
S3 inputs/objects/ → inputs/ready/*.job.json
   → 起動Lambda → 単一AgentCore Runtime
       ├ custom                     独自抽出・Grade
       ├ original_wiki              原本からWiki・OKF
       ├ custom_and_original_wiki   独自抽出＋原本Wiki
       ├ memory_ingest              Memory投入
       ├ memory_wiki                Memory暫定取得→Wiki・OKF
       └ hybrid                     Memory結果→独立Grade
   → S3 results/<job_id>/ のcheckpoint/complete.json
   → DynamoDBの状態管理
EventBridge Scheduler (既定OFF) → 同じLambdaでWAITING_MEMORYのみ再確認
```

原本ファイルを置いただけでは起動せず、`inputs/ready/`の版付きジョブが必要です。`runtime_service/pipeline.py`は既存`copilot_learning/core.py`・`learning.py`・`wiki.py`・`okf.py`を呼びます。`runtime_service/contracts.py`がモード・入力・SHA・モデル許可リストの正式な契約です。

## 現在の安全境界

- Copilot実セッションのイベント構造は社内Windowsで未検証。合成`events.jsonl`の既知メッセージだけ正規化。欠落がある実Runtime入力は拒否。
- Memoryの抽出結果は`derived_memory`、原本Wikiは`raw_document`。引用文字列の一致は主張の真実性を保証しない。WikiはOKF v0.2の`draft`で、Copilotへの自動採用は禁止。
- S3のVersionId/本文SHA、KMS、対象モデルID、Memory profileを固定。Memoryの安定読取を全非同期抽出完了と誤認しない（`complete_for_comparison=false`）。
- `require_online_release()`でオンライン実行停止。S3通知・Schedulerは初期OFF。社内承認、DLP、IAM、KMS、ARM64実起動、実Bedrock/Memoryの検証は未実施。
- Runtimeの6モードは**39比較条件をすべて実行するrunnerではない**。本番の品質・費用は未測定。

## ローカル再現

```powershell
python -m pip install -r requirements-runtime.txt
python .\scripts\demo_offline.py --out "$env:USERPROFILE\learning-demo-01"
python .\scripts\demo_single_runtime.py --out "$env:USERPROFILE\runtime-demo-01"
$env:PYTHONPATH = "tests"
python -m unittest test_memory_resume test_single_runtime test_runtime_aws_and_package -v
```

必ずGit管理外の新規出力先を使用します。デモは合成Memory/モデル/ストレージによるテストで、外部APIは呼びません。詳しくは[現在地](current-status-20261115.md)、[全体構成](architecture.md)、[CodeZip](single-runtime-codezip-20261115.md)。
