# 外部送信準備・適用条件の安全強化（Issue #6 / #7）

2026-11-15。**AWS・Copilotの実機を用いない合成データ向けPoC**。既存の外部送受信停止 `require_online_release()` は解除していません。

## #6: 送信内容をローカルで確定し、元入力と照合する

`copilot_learning/frozen_bundle.py` は `prepare_sessions.py` による原本ZIPとmanifestを再検証し、`build_memory_preflight()` と同じ入力から投入計画を再生成します。`session-<fingerprint>.zip`・manifest・投入計画・preflight・任意feedback・receiptを、**Git管理外**の一つの新規フォルダにコピーします。既存フォルダの上書きは禁止です。

保存した一式は、元のstagingフォルダが変更・消去された後でも検証できます。全ファイルのサイズ・SHA-256、原本から再構成したMemory計画、送信先スコープを照合します。`receipt.json` のSHAを**独立した管理場所**に控えた場合、検証時の`--expected-receipt-sha256`で全体の一致を確認できます。receiptを一緒に書き換えられる攻撃者には、**独立した真正性保証がない**点に注意してください。

```powershell
# オフラインで、事前に作成した合成セッションの入力を確定
python scripts/freeze_memory_offline.py freeze `
  --staging "$env:USERPROFILE\copilot-learning-staging" `
  --fingerprint "<64桁SHA256>" `
  --memory-id "PocMemory-1234567890" --actor-id poc_actor `
  --account-id 123456789012 --region ap-northeast-1 `
  --out "$env:USERPROFILE\copilot-learning-frozen-01"

# 別途控えたreceipt SHA-256と、オフライン確定物を再照合
python scripts/freeze_memory_offline.py verify `
  --out "$env:USERPROFILE\copilot-learning-frozen-01" `
  --memory-id "PocMemory-1234567890" --actor-id poc_actor `
  --account-id 123456789012 --region ap-northeast-1 `
  --expected-receipt-sha256 "<freeze時のreceipt SHA256>"
```

セッションZIPや投入計画は**会話本文を含む可能性があり、保存先は暗号化された社内許可領域を選ぶ必要があります**。ローカルの所有者保護・モード指定は企業管理のDLP/暗号化/署名の代替になりません。確定したファイルでも外部送信は行いません。将来のオンライン移行時には、社内ID署名・IAM・KMS・バケット所有者・不変スナップショットと実PUTを同じ境界に置き、再検証とエラー回復を実機でテストします。

## #7: 意味レビューは判断の「格下げ」だけに使う

`copilot_learning/semantic_gate.py` はTask本文とKnowledge候補の**SHA-256を固定した**レビュー結果JSONをチェックします。否定・適用除外の票には、現在のタスク本文に実在する引用文字列と対応する既存スコープ条件を必須とします。モデルが答えたと主張する適用可能判定は、従来の明示的な適用条件が不足するケースを**勝手に昇格させません**。

これによりモデルの一度きりの判断をCopilotへのSkill反映と混同せず、適用外ケースや反証を次の独立評価に利用できます。実モデルを用いた意味理解の品質（適用精度/誤棄却率）は**未測定**です。意味判定を生成するLLM呼び出しは追加していません。

```powershell
python scripts/review_applicability_offline.py `
  --candidate "<候補JSON>" --task "<タスク本文のテキスト>" `
  --review "<独立レビューレコードJSON>" `
  --out "$env:USERPROFILE\copilot-learning-meaning-result.json"
```

## 今回の範囲外

- 社内SSO/承認主体の検証、DLP、暗号化鍵、AWS上の本番S3保存・復旧
- Copilotセッションの詳細な実ログスキーマへの対応、Copilotへの自動Skills適用
- AgentCore Memory・AgentRun型・Hybridの実推論とモデル比較

`python -m unittest discover -s tests -v`で合成データを使用し、既存93テストを含む検証を行います。実ネットワーク/社内データ/実Copilotは使用しません。
