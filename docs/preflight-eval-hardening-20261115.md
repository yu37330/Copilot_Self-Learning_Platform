# AWS接続なしで進める改善：送信プレビュー・Hold-out評価（Issue #6/#10）

2026-11-15 / **オフラインのみ**。本変更は社内承認、DLP、署名、AWS実機試験を代替しません。v0.3の `require_online_release()` による外部送受信停止を維持しています。

## #6: 外部送信前のデータ同一性・スコープ検証

`copilot_learning/preflight.py` で検証済みのZIP/manifestから投入予定データを **再生成** し、S3送信前の承認台帳に将来結び付けるためのプレビューを作ります。プレビューに会話本文は含めず、`operation_scope` のハッシュ、Memory ID、actor ID、リージョン、AWSアカウント、入力原本SHA、フィードバックのSHA、イベント数を記録します。

- 再検証時にZIP、manifest、feedback、Memory ID、actor ID、リージョン、アカウント、プレビューJSONの変更を拒否します。
- 未確認イベントが残るセッションは、学習の重要情報が脱落する可能性があるためプレビュー生成を拒否します。
- **プレビューは未署名・未承認です**。企業SSO/ID、レビュアー権限、DLP、送信スナップショット、IAM/KMS/S3実機検証は依然として #6 に残ります。

```powershell
# 合成セッションのZIP/manifestをローカルで作った後に実行（AWS通信なし）
python scripts/preflight_offline.py preview `
  --staging "$env:USERPROFILE\copilot-learning-staging" `
  --fingerprint "<セッションSHA256>" `
  --memory-id "PocMemory-1234567890" --actor-id "poc_actor" `
  --account-id "123456789012" --region "ap-northeast-1" `
  --out "$env:USERPROFILE\copilot-learning-previews\preflight.json"

# 入力とプレビューが変わっていないか再検証
python scripts/preflight_offline.py verify `
  --staging "$env:USERPROFILE\copilot-learning-staging" `
  --fingerprint "<セッションSHA256>" `
  --memory-id "PocMemory-1234567890" --actor-id "poc_actor" `
  --account-id "123456789012" --region "ap-northeast-1" `
  --out "$env:USERPROFILE\copilot-learning-previews\preflight.json"
```

実在しないアカウントIDやMemory IDは説明用です。会社PCの実セッションや機密コードは使用しないでください。

## #10: 合成Hold-outの重複検査・安全性指標

`copilot_learning/evals.py` は従来の `training_group_ids` と `cases[].group_id` に加え、`training_task_sha256` と `cases[].task` を利用できます。本文から NFKC・casefold・空白統一でSHA-256を計算し、訓練・評価間の完全／表記揺れ重複と評価内重複を拒否します。

- `leakage_checks.complete` に判定の完全性を出力します。新しいtask本文や訓練SHAを渡さない古い形式は動作しますが、`complete=false` にします。
- `false_application_rate` と `false_rejection_rate` の分母を別々に扱い、`abstention_rate`、陽性/陰性件数、Wilson区間を出力します。
- 自然言語の同義文、同じ障害系列の言い換え、隠れたデータ系列の漏えいは機械的に検出できません。**学習なしD、AgentCore A、AgentRun型B、Hybrid Cの実成績やモデル費用は未測定です。**

```powershell
python scripts/evaluate_offline.py `
  --cases examples/holdout_synthetic.json `
  --predictions examples/holdout_predictions_synthetic.json `
  --out "$env:USERPROFILE\copilot-learning-evals\holdout-demo.json"
```

この例のBの予測は**人手で作った合成ラベル**であり、実際のAgentRunやBedrockが推論した結果ではありません。モデル間の優劣を示すためには使えません。

## 試験と残課題

- `python -m unittest discover -s tests -v` はローカル合成データのみ実行。GitHub Actions / CIなし。
- #6: 社内権限がある本当の承認主体、署名・不変性、DLP、日本語企業名・PII、送信バイト固定と受領再開を引き続き要検証。
- #10: 同義文の漏えい検出、実タスクでのモデル・予算・プロンプト・評価者を固定した比較、実Copilotの適用/不適用成功率・AWSコストは未実装。
