# Copilot Self-Learning Platform

GitHub Copilotの開発履歴を後から検証し、AgentCore Memory標準・AgentRun型・Hybridで知識を抽出・比較するための**オフラインPoC**です。

> **AWS実送受信・モデル呼出しは停止中です。** `-Upload` / `--execute` / `--use-bedrock` では解除できません。署名検証機能があっても、会社の認証・DLPとの実統合が済んだ意味ではありません。Copilotへの自動インストールも行いません。

## 現在の実装範囲

| 範囲 | 状態 |
|---|---|
| セッション選別・ZIP化・SHA検証・イベント正規化 | 合成データで検証。実Copilotのイベント形式は未検証 |
| Memory投入計画・送信データ固定・機密語検査・署名承認検証 | オフライン実装。組織の認証と実送信は未接続 |
| 候補単位の証拠・反証評価、意味レビューの安全側判定 | 実装。実モデルでの精度は未検証 |
| SQLite知識台帳、版管理、日本語検索 | 実装。active化は自己申告レビューであり社内認証ではない |
| A/B/C/Dの共通・ペア比較と検索評価 | 評価器を実装。実サービスを走らせた比較ではない |
| Skillプレビューの出力・再検証 | 実装。既存のSkills/Instructionsに書き込まない |
| WM/WOの共通Wiki出力 | OKF v0.2のdraft、出典・脚注・索引・ハッシュ検査。実LLM編集は未接続 |
| Copilotへの動的注入・Workflow実行 | 未実装 |

**テスト成功や候補のkeep判定は、学習品質向上・社内運用承認を示しません。**
本リポジトリは公開です。会話履歴、社内ソース、台帳、タスク、辞書、署名秘密鍵、承認証跡をコミットしないでください。

## 前提

ローカル処理はPython 3.10以降を想定。署名検証には`cryptography`、WikiのOKF出力・構造検査には`requirements-okf.txt`のPyYAMLが必要です。検証実行はLinux/Python 3.13で、Windows PowerShell・AWS・Copilot実機は未検証です。WSL2/Dockerは初期要件に含めません。CI/GitHub Actionsは使用しません。

## AWS・Copilot不要の確認

リポジトリのルートから実行します。デモ出力先は**Git管理外の新規フォルダ**にしてください。

```powershell
python -m pip install -r requirements-okf.txt
python -m unittest discover -s tests -v
python .\scripts\demo_offline.py --out "$env:USERPROFILE\copilot-learning-demo-01"
python .\scripts\demo_skill_preview.py --out "$env:USERPROFILE\copilot-skill-preview-demo-01"
python .\scripts\demo_wiki_offline.py --out "$env:USERPROFILE\okf-wiki-demo-01"
```

デモは人手で作った合成入力です。実サービスの学習・実行結果ではありません。

## WikiのOKF v0.2対応

AgentCore Memory型(WM)と独自原本型(WO)を共通のOKF形式で出力します。概念ページは`type: Reference`、`status: draft`。出典は`sources`と脚注で対応付け、`references/`に引用抜粋を同梱します。内容を意味的に確認していないため`verified`は付けません。既存の`wiki.json`とSHA検証を残し、形式適合と採用承認を分離します。

```powershell
python .\scripts\validate_okf_offline.py --directory "$env:USERPROFILE\okf-wiki-demo-01\original"
```

旧版は従来どおり検証でき、`wiki_offline.py migrate-okf`で新規フォルダへ移行できます。出力済みの旧ファイルは自動更新しません。[使い方・移行・仕様範囲](docs/okf-wiki-20261115.md)

## Issue #11：Skillプレビュー

知識IDと期待revisionを明示し、activeかつkeepのNote/Skillだけを対象にします。適用外・保留・退役・古い版・機密検出は拒否します。台帳はSQLiteの読取専用接続で参照します。

出力は`SKILL.preview.md`と`manifest.json`だけです。`SKILL.md`へ自動改名せず、Copilotの自動読込先にも配置しません。確認時にはハッシュと現在の台帳状態を再照合します。Markdownだけで実Copilotの権限を制御できるわけではありません。

[使い方・export/verify・制約](docs/skill-preview-20261115.md)

## セッションのローカル準備

```powershell
python .\scripts\prepare_sessions.py --list
python .\scripts\prepare_sessions.py --session "<確認したID>" --out "$env:USERPROFILE\copilot-learning-staging"
```

既定では`~/.copilot/session-state/<session-id>/events.jsonl`を想定。保存形式・終了状態は会社PCで確認が必要です。最終更新からの経過時間だけでは終了を厳密に保証できません。ZIPは既知パターン検査のみで、会社の機密情報・個人情報が完全に除去される保証はありません。

## 設計・検証資料

- [全体構成](docs/architecture.md) / [オフライン学習処理](docs/offline-pipeline.md) / [AgentRunとの詳細比較](docs/agentrun-process-comparison-20261115.md)
- [レビュー修正](docs/review-response-20261114.md) / [知識台帳・検索](docs/offline-improvements-20261115.md)
- [送信前検証・共通評価](docs/preflight-eval-hardening-20261115.md) / [検索品質評価](docs/retrieval-evals-20261115.md)
- [確定バンドル・意味レビュー](docs/frozen-bundle-and-semantic-gate-20261115.md) / [機密検査・適用範囲評価](docs/privacy-semantic-holdout-20261115.md)
- [署名検証・ペア比較](docs/offline-attestation-paired-eval-20261115.md) / [Skillプレビュー](docs/skill-preview-20261115.md)
- [Wiki二方式](docs/wiki-dual-approach-20261115.md) / [OKF対応・移行](docs/okf-wiki-20261115.md)

古い設計文書にあるオンライン手順は現時点では利用できません。会社の正式承認・送信経路の独立検収・実環境試験の完了後に、別変更として再開判断します。Issue #6〜#11は残課題を維持しています。
