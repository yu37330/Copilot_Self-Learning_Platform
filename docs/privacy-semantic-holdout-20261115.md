# #6 / #7 オフライン改善（2026-11-15）

本追加は**AWS・Copilotへの通信なし**で安全性を強化する前処理です。データ所有者の利用許可・必要な情報保護（組織利用ではSSO/DLP）・送信実行は未接続のため、`-Upload`・`--execute`・`--use-bedrock`の安全停止は解除しません。開発環境はWindows以外でも実行可能ですが、実機は未検証です。

## #6 凍結済みセッションの追加プライバシー検査

`privacy_scan.py` と `scan_privacy_offline.py` を追加しました。`freeze_memory_offline.py` で確定したZIP/manifest/Memory投入計画/feedbackについて、独立して控えたreceipt SHAを照合してから、許可対象のテキストを検査します。メールアドレス・日本の携帯電話番号・JWTの形式、および組織固有の機密語（完全一致）を検出し、**一致文字列・会話全文をログ／JSONへ書かずに**ファイル名と種別別の件数だけ記録します。組織辞書は公開Git管理の外に保存し、任意の正規表現は受け付けません。検出ありは`blocked_findings`、0件は`manual_review_required`です。**0件でも自動承認は不可**です。

機密語辞書の例（個人の秘密語や組織専用辞書は、どちらもGit管理外のアクセス制御領域に置く）：

```json
{"schema_version":"1.0","record_type":"offline-sensitive-literals","sensitive_literals":["組織内だけの機密語"]}
```

PowerShell実行例（AWS通信なし）：

```powershell
python scripts/scan_privacy_offline.py `
  --bundle "$env:USERPROFILE\copilot-learning-frozen-01" `
  --policy "$env:USERPROFILE\secure-policy.json" `
  --memory-id "PocMemory-1234567890" --actor-id poc_actor `
  --account-id "123456789012" --region "ap-northeast-1" `
  --expected-receipt-sha256 "<独立保管したreceipt SHA256>" `
  --out "$env:USERPROFILE\copilot-privacy-report.json"
```

結果の`external_transfer_authorized`は常に`false`。検出ありのCLI終了コードは`1`、形式・改ざん等のエラーは`2`、検出ゼロは`0`です。`0`は承認を意味しません。辞書外の顧客名・内部コード、画像に埋め込まれた情報、他形式のPII等を全て検出するものではありません。正しいデータ区分や承認者の権限検証を含む組織のDLPを後続で接続する必要があります。

## #7 未学習データで適用条件／否定票を評価

`semantic_holdout.py` と `evaluate_semantic_holdout.py` を追加しました。タスクごとに正解ラベル、候補Knowledge、入力タスク本文、任意の独立意味レビュー応答を用意し、**明示句による判定（literal）**と**根拠を要求する意味レビューの安全ゲート（evidence_review）**を同じケースで比較します。

- `case_id`, `group_id`, `task`, `expected` (`applicable` / `not_applicable`), `candidate`, `review`（省略時は判定保留）を持つケース配列を使用。
- 学習側の`training_group_ids`と`training_task_sha256`が必要。グループ・正規化本文ハッシュの重複は拒否。
- 生成レポートでは元のタスク・候補本文・レビューの自由記述を出さず、hash、判定、メトリクスだけを出す。
- 誤適用率・誤棄却率・保留率（カバレッジ）・95% Wilson区間を別々に測る。
- 既存のA/B/C/D実方式比較とは区別し、ここでは`literal_baseline` / `evidence_review_gate`という**評価器間の比較**であることを明記。

合成データの例：

```powershell
python scripts/evaluate_semantic_holdout.py `
  --dataset examples/semantic_holdout_synthetic.json `
  --out "$env:USERPROFILE\copilot-semantic-holdout-report.json"
```

結果の`real_llm_quality_verified`、`copilot_improvement_verified`は必ず`false`です。提供されたレビュー応答の検証であり、現段階では**実モデルの意味判断精度や実際のCopilot改善効果を証明しません**。類似ケースが学習・評価に横断するデータリーク、正解ラベルの誤り、実運用でのモデル出力の偏りは別途検証が必要です。

## 合格確認

```powershell
python -m unittest discover -s tests -v
```

既存110件＋追加12件を合成データで検証。AWS実接続、個人/組織の実セッション原本の送信、承認ゲート解除、Copilot Skill書換は行いません。
