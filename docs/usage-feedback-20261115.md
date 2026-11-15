# Issue #11/#8/#10: Skill試行・結果・訂正の回収（オフライン）

## 従来との差

PR #18はプレビューを出力・検証するところまで。本変更は、指定された有効なプレビューを再検証し、その知識ID・版・候補ハッシュ・元セッション・対象タスク・機密辞書に**試行と結果を紐付ける**。

`begin` は試行登録であり、「知識を実際に使った」「成功した」とは数えない。`result` が受け取るのは手動申告であり、Copilotの実行証明ではない。元の知識台帳を更新せず、別のSQLite台帳へ追記する。

```text
有効なSkillプレビュー + タスク + 辞書 + 独立保管manifest SHA
  → begin: プレビュー/版/適用範囲を再検証 → 試行IDとSHA
  → 利用者の結果申告 + 証拠ファイル
  → result: 証拠の既知機密検査とSHA → 結果を追記
  → 訂正: 期待revisionと前結果SHAを指定 → 前記録を保持して追記
  → report: 合成/手動申告を分離集計 → 失敗・保留・訂正を再レビュー対象にする
```

## 判定の意味

| 値 | 意味 |
|---|---|
| `reported_success` | 使用したとの申告と証拠ファイルがある成功申告。真実性や知識の因果効果は未検証 |
| `reported_failure` | 同じ条件の失敗申告。知識を自動退役させず、人手の再レビュー対象 |
| `inconclusive` | 結果不明。成功件数に含めない |
| `not_used` | 未使用。成功・失敗件数に含めない |
| `unreported` | 試行登録のみで結果がない |

記録者・元run ID・証拠はダイジェストだけを保存する。元タスク本文、証拠本文、記録者名、ローカルパスは結果台帳に保存しない。`source_kind=synthetic`と`manual_report`を別集計する。失敗を成功へ訂正しても、履歴と再レビュー要求を残す。

## CLI

下記は例。すべての入出力をGit管理外へ置き、先に `export_skill_offline.py` でプレビューを作る。実データの利用可否はデータ所有者の許諾と適用される規約（組織利用では所属先の規程）に従う。

```powershell
python .\scripts\usage_feedback_offline.py begin `
  --journal "$env:USERPROFILE\learning\usage.sqlite" `
  --db "$env:USERPROFILE\learning\knowledge.sqlite" `
  --preview "$env:USERPROFILE\learning\preview" `
  --task "$env:USERPROFILE\learning\task.txt" `
  --policy "$env:USERPROFILE\learning\policy.json" `
  --expected-manifest-sha256 "<exportが返した64桁SHA>" `
  --run-id "synthetic-run-001" --source-kind synthetic

python .\scripts\usage_feedback_offline.py result `
  --journal "$env:USERPROFILE\learning\usage.sqlite" `
  --trial-id "<beginが返したID>" --expected-trial-sha256 "<beginが返したSHA>" `
  --expected-revision 0 --outcome reported_failure --used `
  --evidence "$env:USERPROFILE\learning\synthetic-test-result.txt" `
  --policy "$env:USERPROFILE\learning\policy.json" --reporter "synthetic-reviewer"

python .\scripts\usage_feedback_offline.py report `
  --journal "$env:USERPROFILE\learning\usage.sqlite"
```

訂正は `--expected-revision 1 --previous-result-sha256 "<直前の結果SHA>"` を指定する。内容が同じ再送は同じ結果を返し、別内容の競合は拒否。既存記録の上書き・削除機能は提供しない。

## 安全上の境界と残課題

- 試行開始時点のactive状態を確認するが、配布・インストール・実行の許可は出さない。
- 試行開始後に知識が退役しても、遅れて到着した結果を記録できる。退役した知識の新規試行登録は禁止。
- 証拠ファイルは存在・サイズ・既知機密検査・ハッシュだけを確認し、内容の正しさやテスト実行の真正性は確認しない。
- SQLite履歴のハッシュは整合性確認であり、同じ権限を持つ利用者の全面書換えを防ぐ監査基盤ではない。本人/組織IDの検証、署名と独立保管は残課題。
- 同一権限の悪意あるファイル差し替えやWindows ACLを保証しない。利用者側のファイル保護が必要。
- 再レビューを要求するだけで、成功による自動active化、失敗による自動退役、候補の書換えは行わない。
- 今回の結果をAgentCoreへ送るアダプター、A/B/C/D実験ランナー、Copilotの自動利用結果取得は未実装。AWSの安全停止を変更しない。
- 新モジュールは標準ライブラリのみ。既存の署名検証テストには引き続きcryptographyが必要。
