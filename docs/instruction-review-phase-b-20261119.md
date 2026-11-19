# Issue #31 Phase B：根拠付き指示改善diffプレビュー（未適用）

更新日：2026-11-19。Phase Aのセッション振り返りを拡張し、元の指示ファイルのSHAとイベント根拠を固定した**オフライン改善案**を作る。Phase Bの本実装は外部LLM接続・自動修正を含まない。

## できること

- 既存のread_archive/normalize_eventsで、検証済みセッションZIPを読み取る
- AGENTS.md、.github/copilot-instructions.md、.github/instructions/*.instructions.md、.github/skills/*/SKILL.md、.github/agents/*.agent.mdを読み取り専用で棚卸しし、元バイトのSHAを固定する
- 根拠IDとファイルSHAだけを含むレビュー要求を生成。元会話や既存指示本文は要求に含まれない
- 人間または承認された環境で別途作成した提案JSONを再検証し、未適用のdiff・証拠・評価案を出力する
- 元セッション、指示ファイル、ポリシー、提案、成果物に変更があれば再検証に失敗する

出力は改善候補であり、根本原因・意味的な正しさ・改善効果を実証したものではない。未確認の場合は提案配列を空にする。既存のInstruction/Skill/Agentを一切書き換えない。

## ローカル操作（Windows PowerShell）

前提：既存のprepare_sessions.pyで作成した検証済みセッションZIP・manifestと、Git管理外の承認済み機密語辞書JSON。実Copilot固有のログ形式は別途実機検証する。

### 1. 根拠付きレビュー要求を作る

~~~powershell
python .\scripts\instruction_review_offline.py request --staging "$env:USERPROFILE\copilot-learning-staging" --fingerprint "<64桁fingerprint>" --repo "C:\path\to\target-repository" --policy "$env:USERPROFILE\copilot-learning\policy.json" --out "$env:USERPROFILE\retro-review-request-01"
~~~

reviewer-request.preview.jsonにはreport_sha256、inventory_sha256、規則と根拠IDが含まれるが、本文は含まれない。

### 2. 人手でレビューして提案JSONを用意する

サンプルのSHA/IDは実値に差し替える。提案本文はセッションの引用をそのまま転記せず、安全性・既存指示との競合・適用範囲を確認する。

~~~json
{
  "schema_version": "1.0",
  "record_type": "offline-retro-proposals",
  "report_sha256": "<reviewer-requestのreport_sha256>",
  "inventory_sha256": "<reviewer-requestのinventory_sha256>",
  "proposals": [{
    "rule_id": "missing_verification",
    "kind": "append_existing",
    "target_path": "AGENTS.md",
    "expected_sha256": "<inventoryのAGENTS.mdのsha256>",
    "evidence_ids": ["<fingerprint>:<event_index>"],
    "hypothesis": "検証条件が曖昧なため未実施と完了を混同した可能性がある",
    "proposed_block": "## 完了条件\n\n未実施テストは未実施と明記する。",
    "expected_benefit": "未検証の完了報告の減少を期待する",
    "validation": "未学習の同種課題で報告内容を検証する"
  }]
}
~~~

kindはappend_existing（既存ファイルへの追記）、create_new（新規ファイル案）、prompt_advice（ファイル変更なし）を許可する。対象ファイルのパスは限定し、証拠IDが既存の検出規則と一致することが必要。新規Skill/Agentはname/descriptionのみの固定frontmatterが必要。

### 3. 差分を生成する（自動適用なし）

~~~powershell
python .\scripts\instruction_review_offline.py grade --staging "$env:USERPROFILE\copilot-learning-staging" --fingerprint "<64桁fingerprint>" --repo "C:\path\to\target-repository" --policy "$env:USERPROFILE\copilot-learning\policy.json" --proposal "$env:USERPROFILE\copilot-learning\offline-proposals.json" --out "$env:USERPROFILE\retro-review-preview-01"
~~~

新規のGit管理外フォルダにreview.preview.json、changes.preview.diff、reviewer-request.preview.json、manifest.jsonを出力する。元の指示ファイルは更新しない。

### 4. 独立保管したmanifest SHAを使って再検証する

~~~powershell
python .\scripts\instruction_review_offline.py verify --staging "$env:USERPROFILE\copilot-learning-staging" --fingerprint "<64桁fingerprint>" --repo "C:\path\to\target-repository" --policy "$env:USERPROFILE\copilot-learning\policy.json" --proposal "$env:USERPROFILE\copilot-learning\offline-proposals.json" --out "$env:USERPROFILE\retro-review-preview-01" --expected-manifest-sha256 "<grade時のmanifest SHA>"
~~~

## 安全境界と制限

- changes.preview.diffには既存指示が含まれる場合がある。公開Gitへ保存しない
- Windows CRLFは安全検査と差分表示時にだけLFへ正規化し、元バイトSHAを保持する。diffをそのまま適用できる保証はない
- 会話の本文はレビュー要求に含まないが、機密検査は既知ルールに限られるため、別途人手・組織DLPが必要
- 外部モデル推論、AgentCore、Copilot自動適用、GitHub Actions/CIは追加しない。require_online_release()による外部実行停止は維持する
- Phase Cのholdout成功率・コスト・手戻りの実測、実LLMによる独立評価は未実装

## 合成テスト

~~~powershell
python -m unittest discover -s tests -p "test_instruction_review.py" -v
python -m unittest discover -s tests -p "test_session_retro.py" -v
~~~
