# Issue #6 / #10: 署名付き承認証跡と4方式ペア評価（オフライン）

更新日: 2026-11-15。AWS・GitHub Copilot・外部認証基盤を使わない合成検証。対象はWindows PCのGitHub Copilot利用者（個人・チーム・企業）。

## 1. 既存設計との関係

- `freeze_memory_bundle` でZIP・manifest・Memory計画・フィードバックを固定し、`receipt.json` でSHA-256を管理する。
- `scan_frozen_bundle` は既知の秘密情報パターンと利用環境固有語の**オフライン検出器**。検出0件でも利用者/組織の正式な情報保護確認を代替しない。
- 新しい `verify_attestation` は **Ed25519の署名・独立保管されたtrust-store SHA-256・確定receipt・DLPレポート・操作scope** を検証する。
- 署名確認の成功は「署名者がそのデータの利用・外部送信を許可する権限を持つ」と同義ではない。発行鍵、データ所有者/組織の権限、適用規約と必要な情報保護の検証は**別工程**で、現行コードの`organization_identity_verified=false`は維持する。
- **AWS送受信は引き続き停止**。`require_online_release()` は解除しない。承認検証結果も `external_transfer_authorized=false`。

## 2. 署名承認の入力契約

個人/組織の秘密語・証跡と、承認用の鍵情報を**公開Gitリポジトリの外**に保存する。PoCでは合成鍵をテスト時に一時生成するだけで、実際の承認鍵や秘密鍵は保持しない。

受け入れるデータは以下の4種類。

| データ | 必須の検証 |
|---|---|
| 確定バンドル | `receipt.json` の独立SHA、ファイル全部のSHA、Memory送信計画の再生成、scopeの再検証 |
| 機密検査レポート | receipt/sessionとの一致、`findings_total=0`、`status=manual_review_required`、未承認フラグ |
| 信頼設定 | 別経路で渡す固定SHA-256、Ed25519公開鍵、許可操作、機密辞書ポリシーSHA |
| 承認証跡 | 鍵ID・レビュー主体・receipt/report/policy/scopeのSHA、操作、期限、Ed25519署名 |

`signature_base64` フィールド以外の承認証跡JSONを、`json_bytes()` の正規化UTF-8で署名する。期限は最大7日、現在時刻が範囲外なら拒否。署名・信頼設定・対象scope・DLP結果が1つでも異なれば **失敗終了**する。

検証用CLI（**検証のみ・送信なし**）：

```powershell
# trust_store.json / attestation.json / privacy_report.json はGit外に保存
python .\scripts\verify_attestation_offline.py `
  --bundle "$env:USERPROFILE\learning-frozen" `
  --privacy-report "$env:USERPROFILE\learning-private\privacy_report.json" `
  --attestation "$env:USERPROFILE\learning-private\attestation.json" `
  --trust-store "$env:USERPROFILE\learning-private\trust_store.json" `
  --receipt-sha256 "<外部保管した64桁SHA>" `
  --pinned-trust-sha256 "<別経路で確認したtrust SHA>" `
  --memory-id "<承認対象のMemory ID>" `
  --actor-id "<actor>" --account-id "<12桁Account ID>" `
  --region ap-northeast-1 `
  --out "$env:USERPROFILE\learning-private\attestation-check.json"
```

署名検証には `cryptography` が必要（オフラインの他のモジュールは原則標準ライブラリ）。秘密鍵をプロジェクトに保存しない。正式利用時はデータ所有者や組織が信頼する鍵・本人確認・承認ワークフローを接続し、組織利用では必要に応じSSOも組み込む。

## 3. A/B/C/Dペア評価の入力契約

`paired_experiment.py` は同じ未学習タスクをA/B/C/Dの4方式に与えた**提出済み観測結果**を照合する。実際の学習やLLM実行はしない。

- Dataset: `cases[{case_id,group_id,task,expected}]`、`training_group_ids`、`training_task_sha256`、`label_source`を必須にする。
- 各arm: `source_kind=synthetic|imported_replay`、`profile={model_id,budget_tokens,tool_policy}`、`knowledge_revision_sha256`、全caseの予測と `trace_sha256`,`run_id`,`cost_usd`。
- **未実行はarm=null**。Dは知識なしなので `knowledge_revision_sha256=null` とする。A/B/Cは版をSHAで固定する。
- 実行済み方式はケース数・ID・モデル・トークン予算・ツール権限を共通にする。実行IDは重複禁止。学習と評価の同一グループ/タスクハッシュを拒否。
- 予測には `applicable`, `not_applicable`, `needs_review` の3つを使用する。保留は正解に含めない。`accuracy`、`false_application_rate`、`abstention_rate`、`Wilson 95%`を既存評価器で集計する。
- **ペアごと**に「左だけ正しい」「右だけ正しい」「両方正しい」「両方不正解／保留」と正解率差を計算。結果はケース本文・モデル出力全文を含まない。
- ラベルやrun/traceは利用者が提出する情報であり、実際にCopilotが実行したことを自動証明しない。`real_llm_quality_verified=false`、`real_copilot_improvement_verified=false`。

CLI（**集計のみ・送信なし**）：

```powershell
python .\scripts\evaluate_arms_offline.py `
  --dataset .\examples\paired_dataset_synthetic.json `
  --observations .\examples\paired_observations_synthetic.json `
  --out "$env:USERPROFILE\learning-eval\paired-report.json"
```

結果の `all_four_observed` は4方式に観測値が提出されたことだけを示し、比較結果の優劣や信頼区間の統計的有意性を保証しない。

## 4. 残る外部・利用環境の検証

- **#6**: 管理者によるEd25519公開鍵の発行/失効/信頼、本人/利用主体の確認と承認範囲、必要なDLP（組織利用の場合）、保存先IAM/KMS/Block Public Access/監査、変更不能の証跡、Windows実機、AWS実接続。**このPoCから送信を可能にしない。**
- **#10**: Memory Episodicの非同期Reflection、AgentRun型独自LLM処理、Hybrid、知識なしDを実際に同一データで走らせるrunner、コスト・セッション証跡、学習/評価の系列単位の漏えい対策、実Copilotの未知タスクでの効果検証。
- **安全性**: PII/社外秘の自動検査は完全ではない。レビュー担当が見るレポートは原文を含まないが、元のファイルはアクセス制御が必要。

## 5. テスト

`python -m unittest discover -s tests -v` で既存のテストに加え、署名差し替え、期限外、信頼設定改ざん、DLPレポート改変、異なるMemory/actor/region、A/B/C/Dケース欠落、モデル/予算不一致、重複run、train/testリーク、未実行Nullを検証する。GitHub Actions/CIは使わない。
