# オフライン学習基盤の改善（2026-11-15）

対象Issue: [#7 適用条件](https://github.com/yu37330/Copilot_Self-Learning_Platform/issues/7)、[#8 知識台帳](https://github.com/yu37330/Copilot_Self-Learning_Platform/issues/8)、[#9 日本語検索](https://github.com/yu37330/Copilot_Self-Learning_Platform/issues/9)、[#10 比較評価](https://github.com/yu37330/Copilot_Self-Learning_Platform/issues/10)。

## 今回の位置づけ

既存のセッションZIP・候補JSONを置き換えない追加モジュールです。AWSやCopilotの実行環境を必要としません。**外部通信はv0.3と同様に安全停止したまま**です。合成データによるオフラインテストの通過は、学習精度の実証ではありません。

### 1. 適用条件 / 適用除外条件

`copilot_learning/applicability.py` と `learning.py` で、候補に任意の `applies_when`、`not_when`、`counterexamples`（文字列リスト）を持たせられます。候補ハッシュに明示範囲を含め、条件が変わった後に以前の成功フィードバックを流用できないようにします。**旧候補で条件が存在しない場合は従来のハッシュを維持**します。

`evaluate_applicability(candidate, task)` は `applicable / not_applicable / needs_review` と理由を返します。明示的な除外語句が一致したら `not_applicable`、適用条件と除外境界が揃っていて全条件が文字列として一致する場合だけ `applicable`、それ以外は `needs_review` です。**文字列一致は意味理解ではありません。 `applicable` でもCopilotへ自動提供しません。**

### 2. 知識台帳

`copilot_learning/knowledge_store.py` はPython標準SQLiteを使い、候補を `proposed` で登録します。人間の明示操作を経て `pending` → `active`、後日 `retired / superseded` に遷移できます。revision番号によるCAS、履歴、重複防止、原本SHAを持ち、active以外は検索しません。

```powershell
# Git管理外にDBを置く。以下のJSONは合成・社内承認済みデータのみ使用
python scripts/knowledge_offline.py --db "$env:USERPROFILE\copilot-knowledge\knowledge.sqlite" register `
  --candidate "<gradeされた候補1件のJSON>" --source "<原本SHAを含むJSON>"

# 状態変更（SSO本人認証は未実装。社内正式承認の代用にはならない）
python scripts/knowledge_offline.py --db "$env:USERPROFILE\copilot-knowledge\knowledge.sqlite" transition `
  --id "<knowledge_id>" --to pending --expected-revision 1 `
  --reviewer "<識別子>" --reason "確認開始" --confirm-manual-review

# activeだけを検索。候補提案であり自動実行しない
python scripts/knowledge_offline.py --db "$env:USERPROFILE\copilot-knowledge\knowledge.sqlite" search `
  --task "Pythonの仮想環境が原因でテストが失敗した" --path src/test_python.py
```

原本の出典 `source` には最低限 `session_fingerprint` と `archive_sha256`（各64文字のSHA-256）が必要です。知識台帳の`active`は**ローカル状態のみ**でCopilot承認・配布とは別です。

### 3. 日本語対応検索

`copilot_learning/retrieval.py` は英数字トークンと日本語文字2-gramを組み合わせたBM25です。タイトル、説明、本文、適用条件、参照ファイルパスに重みを付けます。候補が明示的な適用除外に一致したときは除外し、根拠不足は `needs_review` として返します。上位結果も `approved_for_copilot: false` です。語彙辞書や意味検索は将来の比較対象です。

### 4. Held-out評価の下地

`copilot_learning/evals.py` と `scripts/evaluate_offline.py` は合成データの適用/非適用予測だけを比較します。`training_group_ids` とhold-outの`group_id`重複は禁止。方式A/B/C/Dの予測がない場合は `status: not_executed`、精度はnullとし、0%扱いしません。誤適用率とWilson 95%区間を計算します。

```powershell
python scripts/evaluate_offline.py --cases "<holdout_cases.json>" `
  --predictions "<predictions_by_method.json>" `
  --out "$env:USERPROFILE\copilot-evals\scope-report.json"
```

この値は**知識の適用ラベル分類の合成評価**です。AgentCore、AgentRun型、Hybridの実サービス品質やCopilotの成功率・コスト改善を示すものではありません。

## 残課題

- #6 承認主体・DLP・不変S3スナップショットとAWS接続の完成。通信停止は継続。
- #7 適用条件の意味推論、評価用の適用除外ケース拡充、候補ごとの人間の監査。
- #8 実組織の署名/SSO承認、Git/クラウド台帳との同期と再現性検証。
- #9 複数プロジェクトでの検索指標、形態素解析/埋め込み手法との比較。
- #10 同一データでA/B/Cを実際に動かす検収、Copilotに知識を戻した際の実効評価。
- #11 Copilotへの読み取り専用再利用・Workflow DSL・自動実行。未実装。
