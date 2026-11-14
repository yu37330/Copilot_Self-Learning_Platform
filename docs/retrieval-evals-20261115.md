# Issue #9：日本語知識検索のオフライン評価

更新: 2026-11-15。`copilot_learning/retrieval_evals.py` は **active** 状態の知識だけを検索し、検索結果を人が付けた適用・禁止ラベルと比較します。AWS・Copilotへは通信しません。

## 入力

Git管理外のSQLite知識台帳を作成し、合成候補を登録した後、`proposed → pending → active` の手動遷移を行います。評価ケースは次のようなJSONです。

```json
{
  "label_source": "synthetic_hand_labels_only",
  "training_group_ids": ["train-project"],
  "cases": [
    {
      "case_id": "synthetic-env-1",
      "group_id": "holdout-environment",
      "task": "Pythonの仮想環境が一致しない",
      "changed_paths": ["tests/test_env.py"],
      "relevant_ids": ["<知識台帳のactiveなID>"],
      "forbidden_ids": []
    },
    {
      "case_id": "synthetic-api-1",
      "group_id": "holdout-api",
      "task": "APIの戻り値仕様が変更された",
      "relevant_ids": [],
      "forbidden_ids": ["<適用すべきでないactiveなID>"]
    }
  ]
}
```

`relevant_ids`と`forbidden_ids`は、**評価者が事前に付けたラベル**でなければなりません。検索結果を見てからゴールドラベルを調整すると評価が歪みます。知識IDは例のプレースホルダを置き換えます。ラベルに非active IDがある場合は評価を停止します。

```powershell
python scripts/evaluate_retrieval_offline.py `
  --db "$env:USERPROFILE\copilot-knowledge\knowledge.sqlite" `
  --cases "$env:USERPROFILE\copilot-evals\retrieval-cases.json" `
  --top-k 5 `
  --out "$env:USERPROFILE\copilot-evals\retrieval-report.json"
```

## 評価指標

- `hit_rate_at_k`: 正例のうち上位k件で少なくとも1つの関連知識を見つけたケースの比率
- `mrr_at_k`: 正例における最初の関連知識の逆順位の平均
- `negative_case_exposure_rate`: 正例を持たないケースなのに検索候補が返った割合（`needs_review` も候補としてカウント）
- `forbidden_knowledge_exposure_rate`: 禁止ラベル付き知識が検索候補に混入したケースの割合
- `knowledge_revision_sha256`: active知識のID・revision・候補ハッシュによる再現性識別子

これらは検索候補の指標で、**Copilotが実際に間違えた率ではありません**。`search_active()` の返す `approved_for_copilot: false` は維持され、Skill自動投入も行いません。日本語の意味推論や言い換え判断は実装していないため、文字n-gram一致による検索がどの程度の関連性を持つか、別の案件・人手ラベルで検証する必要があります。

## 今後の拡張

- 日本語の形態素解析・埋め込み検索を同じ正解集合で比較
- 適用範囲の意味判定を評価する別指標（#7）
- 検索した知識を次のCopilotタスクに **読み取り専用** で渡す経路（#11）
- 本番データの外部通信と社内本人承認・DLPは #6 が完了するまで停止
