# Issue #11: Copilot向けSkillプレビュー（オフライン・手動取込み前の段階）

実装日: 2026-11-15。対象Issue: #11、関連: #6/#7/#8/#9。既存のAWS I/O停止は解除しない。

## 変更の目的と範囲

従来は候補の評価・台帳登録・検索までで、知識をCopilot向け形式に出す経路がなかった。
今回、**明示的に指定したactiveなNote/Skillの1件**を、特定タスクの参考資料として出力する。
検索結果の自動取込み、Copilotの設定変更、Skillsのインストール、モデル呼出しは行わない。

GitHub公式資料ではSkillは`SKILL.md`、YAMLの`name`/`description`、Markdown本文で構成される。
ただし現段階は配布承認が未完成のため、あえて **`SKILL.preview.md`** という非標準名を使い、
`.github`/`.agents`/`.claude`/`.copilot`およびGit管理下への出力も拒否する。
公式参照: https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-skills

`allowed-tools`、実行スクリプト、MCP設定、Hook、Workflowは出力しない。
本機能でいう「読み取り専用」は**台帳を読む側の実装**を意味する。
Markdownの文言だけで、インストール後のCopilotのツール権限を制限できるわけではない。

## 処理フロー

```text
明示したknowledge_id + expected_revision
  → SQLiteをmode=ro/query_onlyで読取（DBを新規作成・更新しない）
  → active/keep/非Workflow、現在値と最新レビュー履歴、候補SHA、原本・引用IDを照合
  → 適用/除外条件、任意の証拠付き意味レビューを検査
  → 既知Secret/メール/電話/JWT/組織辞書/危険指示/制御文字を検査
  → name/descriptionのみの固定frontmatter、引用した知識と適用条件を生成
  → 新規のローカルフォルダへSKILL.preview.md + manifest.jsonを出力
  → 後でverify：独立保管manifest SHA + 再レンダリング + 現在の台帳状態を照合
```

`keep`でも`pending`/`retired`/`superseded`なら出力しない。
台帳がactiveでも候補の判定がpendingなら出力しない。原本はダイジェストと引用の対応を確認するが、
原本ZIPそのものや署名済みの正式な利用承認を今回読み直して検収するわけではない。
適用判定が`needs_review`/`not_applicable`なら保留し、指定条件・対象タスク・機密辞書の変更は再生成を要求する。

## コード

| ファイル | 責務 |
|---|---|
| `copilot_learning/skill_preview.py` | 台帳の読取専用照合、プレビュー生成、出力、再検証 |
| `scripts/export_skill_offline.py` | `export`/`verify` CLI。本文やパスをエラーに出さない |
| `scripts/demo_skill_preview.py` | 合成知識だけで登録から再検証までのデモ |
| `tests/test_skill_preview.py` | 状態・根拠・秘密・改変・出力先・通信0件の回帰検査 |

## AWS・Copilot不要のデモ

リポジトリのルートから、Git管理外の**新規フォルダ**を指定する。

```powershell
python -m unittest discover -s tests -v
python .\scripts\demo_skill_preview.py --out "$env:USERPROFILE\copilot-skill-preview-demo-01"
```

生成物は合成の台帳、タスク、辞書、`preview/SKILL.preview.md`、`preview/manifest.json`、`demo-result.json`。
合成候補は人手で用意したもので、AgentCore/LLMが学習して生成した結果ではない。

## 既存台帳から1件選ぶ

`--knowledge-id`と`--expected-revision`は台帳の実際の値を指定する。
入力と出力はデータ所有者の権限範囲で保管し、組織利用では所属先のデータ保護ルールにも従う。公開GitHubへ追加しない。

```powershell
python .\scripts\export_skill_offline.py export `
  --db "$env:USERPROFILE\copilot-learning\knowledge.sqlite" `
  --knowledge-id "<24桁の知識ID>" --expected-revision 3 `
  --task "$env:USERPROFILE\copilot-learning\task.txt" `
  --policy "$env:USERPROFILE\copilot-learning\policy.json" `
  --out "$env:USERPROFILE\copilot-skill-preview-01"
```

出力JSONの`manifest_sha256`を別に保管し、手動レビュー前に同じタスク・辞書で照合する。

```powershell
python .\scripts\export_skill_offline.py verify `
  --db "$env:USERPROFILE\copilot-learning\knowledge.sqlite" `
  --task "$env:USERPROFILE\copilot-learning\task.txt" `
  --policy "$env:USERPROFILE\copilot-learning\policy.json" `
  --out "$env:USERPROFILE\copilot-skill-preview-01" `
  --expected-manifest-sha256 "<exportが返した64桁SHA>"
```

`--review`を指定した場合、verifyにも同一レビューJSONが必要。`--max-bytes`は出力上限（既定16KiB）で、超過時は切り詰めず拒否する。
CLIは成功0、入力不正・拒否2。**成功0は送信許可・意味的正しさ・利用者/組織の正式な承認を意味しない。**

## 制約と残課題

- active化のレビュー主体は自己申告のまま。署名付き送信承認とは未統合。
- 適用条件は文字列照合。意味レビューも肯定による昇格はできず、正しさの保証にはならない。
- 機密検査は既知ルールのみ。誤検出・未検出があり、組織のDLPや個人情報保護の確認を代替しない。
- 出力に元タスクやレビュー理由の本文は書かないが、採用知識の本文は含まれる。ディスク暗号化/ACLが別途必要。
- ファイルモードは事故軽減策。Windows ACL、同権限の悪意ある書換え、署名済み本人性は未検証。
- verifyは実行時点での状態確認。あとからの退役を既にコピーされたファイルへ自動反映する機能はない。
- 別フォルダで手動コピー・改名した場合まで自動読込防止を保証しない。追加skill検索パスへの登録も禁止。
- `SKILL.md`としての本配布にはデータ所有者の明示的な許可と対象Copilot実機での確認が必要（組織利用では別途管理者承認）。今回の変更では自動改名しない。
- Workflowの実実行、Session開始時の動的注入、実Copilotからの自動使用記録回収は未実装。手動の使用実績・訂正記録は[usage-feedback](usage-feedback-20261115.md)に追加済みですが、本番からの自動収集ではありません。Issue #11はOPENを維持する。
