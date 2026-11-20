# Copilot Self-Learning Platform

GitHub Copilotの開発履歴を後から検証し、AgentCore Memory標準・AgentRun型・Hybridで知識を抽出・比較するための**オフラインPoC**です。

**想定利用者：Windows PCでGitHub Copilot（CLI／デスクトップアプリ）を使う開発者。個人・チーム・企業いずれも対象です。** セッションの保存形式はCopilotの製品/版によって異なり、対象Windows実機での検証はまだ行っていません。

> **既存AWSルートの実送受信・モデル呼出しは停止中です。** `-Upload` / `--execute` / `--use-bedrock` では解除できません。個人PCでは、別の[自宅用モデルルート](docs/personal-local-model-routes-20261120.md)から OpenAI API / Codex CLI / GitHub Copilot CLI を明示的に実行できます。データの確認と送信先の認証設定が必要です。組織利用では所属先の規程・SSO/DLP等も確認してください。Copilotへの自動インストールは行いません。

## ライセンス

このリポジトリの独自コードと関連ドキュメントは、**[MIT License](LICENSE)** で公開します。著作権者の表示とライセンス文を保持する条件で、**利用・改変・コピー・再配布・商用利用が可能**です。改変版を別の条件で配布することもできます。無保証のライセンスです。

第三者のPythonライブラリやAWS/GitHubなどの外部サービスには、それぞれの利用条件・ライセンスが適用されます。依存パッケージを含むCodeZipには、このプロジェクトの`LICENSE`も同梱するようにしています。**MITの付与は個人・組織の非公開セッションデータや秘密情報の公開許可を意味しません。**

## 最新の構成・ドキュメント

**2026-11-15 / PR #27まで：単一CodeZip Runtime、S3 ready→Lambda起動とMemory待ち自動再開のコードを実装しています。** Managed Harnessは不採用。AWS実デプロイ・権限確認済みオンライン実行は未実施です。

[**現状・残課題**](docs/current-status-20261115.md) → [**最新アーキテクチャ**](docs/architecture.md) → [**Runtime/CodeZip**](docs/single-runtime-codezip-20261115.md) → [**Memory再開**](docs/memory-resume-20261115.md)。比較は [39条件の準備計画](docs/full-experiment-suite-20261115.md)、Wikiは [OKF v0.2](docs/okf-wiki-20261115.md) に整理しました。

## 現在の実装範囲

| 範囲 | 状態 |
|---|---|
| セッション選別・ZIP化・SHA検証・イベント正規化 | 合成形式・CLIネイティブ形式に加え、VS Codeの公式toExport JSONの限定テキスト部分から安全なZIPを生成。合成テスト済み。実Copilot利用者データでは未検証 |
| Codexのローカルrollout取込 | Issue #46: 公式Codexのrollout JSONLから限定テキストを安全に抽出し、Session Retrospectiveへ接続。実Codex保存先はメタデータのみ確認、本文の互換性は未検証 |
| Memory投入計画・送信データ固定・機密語検査・署名承認検証 | オフライン実装。組織の認証と実送信は未接続 |
| 候補単位の証拠・反証評価、意味レビューの安全側判定 | 実装。実モデルでの精度は未検証 |
| SQLite知識台帳、版管理、日本語検索 | 実装。active化は自己申告レビューであり本人/組織による正式承認ではない |
| A/B/C/Dの共通・ペア比較と検索評価 | 評価器を実装。実サービスを走らせた比較ではない |
| Skillプレビューの出力・再検証 | 実装。既存のSkills/Instructionsに書き込まない |
| セッション振り返り・指示改善 | Issue #31 Phase A: 兆候抽出、Phase B: 指示改善diffのオフライン独立検査、Phase C: 変更前後の未学習タスク比較・費用/手戻りの提出済み観測集計。実Copilotログ・LLM・改善効果は未検証 |
| 第3系統：ChatGPT Task Improvement | Issue #41: ChatGPT会話の選択分岐＋Desktop Commander実行履歴をGit管理外で匿名集計。共通Task IDの自動紐付け・実業務成功判定・LLM評価は未実装 |
| WM/WOの共通Wiki出力 | OKF v0.2のdraft、出典・脚注・索引・ハッシュ検査。Runtimeのモデル呼出経路は実装、実LLMは未検証 |
| 自宅用モデルルート | OpenAI API / Codex CLI / GitHub Copilot CLI をknowledge/retrospectiveの生成・評価役ごとに選択。単発実行・結果検証・JSON抽出を実装。実モデルでの認証・応答品質は未検証 |
| 単一Runtime（CodeZip）・S3起動Lambda | HTTP入口、既存処理の統合、依存同梱ZIP、CFN定義・モック結合検証。AWS実デプロイ未実施 |
| Copilotへの動的注入・Workflow実行 | 未実装 |

**テスト成功や候補のkeep判定は、学習品質向上・データ利用や配布の承認を示しません。**
本リポジトリは公開です。Copilot会話履歴、非公開ソースコード、個人情報、台帳、タスク、辞書、署名秘密鍵、承認証跡をコミットしないでください。

## 前提

既存ローカル処理はPython 3.10以降、CodeZipと今回のRuntime検証はPython 3.13を対象。署名検証には`cryptography`、WikiのOKF出力・構造検査には`requirements-okf.txt`のPyYAMLが必要です。検証実行はLinux/Python 3.13で、Windows PowerShell・AWS・Copilot実機は未検証です。WSL2/Dockerは初期要件に含めません。CI/GitHub Actionsは使用しません。

## AWS・Copilot不要の確認

リポジトリのルートから実行します。デモ出力先は**Git管理外の新規フォルダ**にしてください。

```powershell
python -m pip install -r requirements-okf.txt -r requirements-runtime.txt
python -m unittest discover -s tests -v
python .\scripts\demo_offline.py --out "$env:USERPROFILE\copilot-learning-demo-01"
python .\scripts\demo_skill_preview.py --out "$env:USERPROFILE\copilot-skill-preview-demo-01"
python .\scripts\demo_wiki_offline.py --out "$env:USERPROFILE\okf-wiki-demo-01"
```

デモは人手で作った合成入力です。実サービスの学習・実行結果ではありません。

## AgentCore Memoryの設定（AWS未設定の設計案）

詳細は[**AgentCore Memory設定ガイド**](docs/agentcore-memory-recommendations-20261115.md)、構成候補は[**設計JSON**](examples/agentcore_memory_profile.design.json)に記載しています。Episodicを基準とし、Semantic・Summary・User Preference・カスタムoverrideを別条件で比較。短期イベント保持30日、長期記憶保持90日は**提案値**で、作成済みのMemoryへ適用した値ではありません。検索上限とnamespace分離の詳細もガイドにあります。

設計JSONはCreateMemory APIリクエストではありません。Runtimeの`MEMORY_PROFILES_JSON`は、**作成済みのMemoryリソースとstrategy/actor/namespaceをジョブに割り当てる設定**で、Memoryを作成するものではありません。`EnableMemoryAccess`の既定値は`false`です。

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

## Session Retrospective：指示ファイル・Skills改善の候補（Issue #31）

既存の検証済みセッションZIPから、ユーザーの明示的な訂正、繰り返し指示、役割分担の誤解、ツール失敗等をオフラインで分析します。改善先としてAGENTS.md、Copilot instructions、Skills、custom agents、普段のプロンプト、テストの**検討候補**を挙げます。実際の指示ファイルは変更せず、元セッションの文章も出力しません。Chronicle連携・LLMによる原因推定はまだありません。

```powershell
python .\scripts\session_retro_offline.py --staging "$env:USERPROFILE\copilot-learning-staging" --fingerprint "<64桁SHA>" --out "$env:USERPROFILE\copilot-retro-preview-01"
```

結果はretrospective.preview.json、RETROSPECTIVE.preview.md、manifest.jsonです。独立保管したmanifest SHAを使った `--verify` にも対応。Git管理外の新規出力先のみ許可し、自動適用は行いません。詳細は[使用方法・制約・今後の実装](docs/session-retro-20261119.md)。合成回帰テストのみ完了し、実Copilotセッション形式への適合性は未検証です。

### Issue #31 Phase B：既存指示との差分プレビュー

現在のAGENTS.md・Copilot instructions・Skills・custom Agentsを読取専用で棚卸しし、セッションの根拠IDと現行ファイルのSHAに紐づけた改善提案JSONを独立検査します。生成されるdiffはレビュー用で**自動適用しません**。レビュー要求に元セッションや指示ファイルの本文は含まず、実LLMの呼出しも行いません。

```powershell
python .\scripts\instruction_review_offline.py request --staging "$env:USERPROFILE\copilot-learning-staging" --fingerprint "<64桁SHA>" --repo "C:\path\to\repository" --policy "$env:USERPROFILE\copilot-learning\policy.json" --out "$env:USERPROFILE\retro-review-request-01"
```

続く手動提案作成、gradeによるdiff出力、verifyによる版固定の再検証は[Phase B手順](docs/instruction-review-phase-b-20261119.md)を参照してください。WindowsのCRLFの検査、危険指示・出典不一致の拒否、Git管理外出力の制限を追加しています。すべてオフラインの合成データによる検証であり、実際の品質改善の立証ではありません。

### Issue #31：知識学習・Retrospectiveの独立LLMレビュー設定

生成LLMと独立評価LLMを2系統で分ける設定・未信頼レビューの検査を追加しました。知識学習CLIの生成/評価Providerの共有を禁止し、Runtimeのcustom系でも同じモデルIDを拒否します。Session RetrospectiveはPhase Bの検証済み改善案から別モデルのレビュー依頼を作り、持ち込まれた結果のSHA・ID・危険文言を照合します。評価モデルの「支持」も人間の確認待ちで、AGENTS.mdやSkillsは**自動変更しません**。

```powershell
python .\scripts\dual_model_review_offline.py plan --model-plan .\examples\dual_track_models.synthetic.json --out "$env:USERPROFILE\dual-review-plan-01"
```

上の例は合成モデルIDで、実Claude/Nova/GPT等のモデル選定・API接続・外部送信を意味しません。設定では生成と評価のmodel_idを別に指定します。詳しくは[モデルルーティングと独立レビュー手順](docs/dual-model-review-20261120.md)を参照してください。

独立Reviewerの意見を検査した後、`scripts/finalize_retro_review_offline.py`で未適用のdiffへ集約する最終段階も追加しました。評価が`revise`/`reject`ならdiffを出力せず、`support_for_manual_review`でも正式採用しません。元の証拠・指示ファイル・レビュー結果・出力は、独立保管したmanifest SHAで再検証できます。コマンド例と制限は同じ手順書に記載しています。

### Issue #31 Phase C：指示改善の効果を未学習タスクで比較

AGENTS.md・Skillsの変更前後を、同じ未学習タスク、モデル、ツール権限、推論予算で評価した**提出済み観測値**からペア集計します。成功/失敗/保留、ツール呼出回数、手戻り回数、token/費用、遅延の違いを示し、実際のCopilot実行や改善の因果性を実証したとは扱いません。未実行・未計測はnullを維持し、訓練グループとの重複を拒否します。

```powershell
python .\scripts\demo_instruction_effect.py --out "$env:USERPROFILE\copilot-retro-phase-c-demo-01"
```

デモは完全な合成データであり、実Copilot/LLM/APIを呼ばずに比較レポートを確認できます。実測ファイルの `blank` / `evaluate` CLI と詳細な入力形式は[Phase C評価ガイド](docs/instruction-effect-phase-c-20261119.md)を参照してください。実際のセッションログ形式確認、計測runner、実LLMによる根本原因分析は未完了です。

### Issue #36：ネイティブCopilot CLIのイベント取り込み

CLIの実セッションでは type=user.message / assistant.message / tool.execution_complete 等の形式が使われます。既存の平坦なmessageイベントとは分けて正規化する対応を追加しました。元の tool 出力は取り込まず、失敗イベントのみ固定のエラー兆候に置き換えます。タイムスタンプとイベント証拠ID・元ZIPのSHAを保持し、未知形式・機密・混合形式は保留します。

```powershell
# 本文を読まず保存先の有無・件数だけを確認
python .\scripts\probe_session_sources.py

# CLIセッションが存在し、対象データの利用が許可されている場合
python .\scripts\prepare_sessions.py --list
```

詳細は[ネイティブCLI形式・安全制約](docs/cli-session-ingest-20261119.md)を参照してください。対象WindowsのCLI保存先は実機で未確認（現時点で既定フォルダは不在）です。VS Codeの Chat: Export Chat... JSONはCLIと異なる構造です。公式ソースの ChatModel.toExport() で確認できた発言・Markdown応答だけを、**明示的な同意とローカル機密辞書の下で**取り込む限定アダプターを追加しました。ツール実行・内部コンテキスト・未知の応答型は本文として取り込みません。利用手順は[VS Code手動JSON取込](docs/vscode-export-offline-ingest-20261119.md)を参照してください。実セッション形式・機密データ受入はまだ未検証です。

### 第3系統：ChatGPT Task Improvement（Issue #41）

ChatGPTの公式データエクスポートに含まれる会話JSONとDesktop Commanderのtool-history.jsonlを、利用者が指定したローカルパスから読み取って匿名集計します。会話原文・実行コマンド・ツール出力はプレビューへ転載しません。**同じ時刻の会話とツール実行を同一タスクとは推測しません**。共通Task ID・GitHubの成果物・業務完了検収は後続実装です。

    python .\scripts\analyze_chatgpt_desktop_offline.py analyze --chatgpt-json "$env:USERPROFILE\private-export\conversations.json" --desktop-history "$env:USERPROFILE\.claude-server-commander\tool-history.jsonl" --policy "$env:USERPROFILE\private-export\policy.json" --out "$env:USERPROFILE\chatgpt-task-review-01" --acknowledge-local-analysis

入力は機密を含みうるためGit管理外で管理し、明示的な同意が必要です。ChatGPTエクスポートの全バージョンには対応していません。機密辞書、入力形式、独立ハッシュ再検証、限界は[第3系統の利用ガイド](docs/chatgpt-task-improvement-20261120.md)を参照してください。合成テストのみ検証済みです。

### Issue #41 Phase 2：ChatGPT会話とDesktop Commanderの明示Task紐付け

匿名集計に加え、ChatGPT会話のIDハッシュとDesktop Commander呼び出しの通し番号・元レコードSHAをインデックス化し、ユーザーが明示的に定義したTask IDの範囲だけを紐づけられます。時刻が近いだけでは同一業務と判断せず、**報告された完了状態も外部成果物で検証するまでは未確認**です。

    python .\scripts\bind_chatgpt_task_trace_offline.py index --chatgpt-json "$env:USERPROFILE\private-export\conversations.json" --desktop-history "$env:USERPROFILE\private-export\tool-history.jsonl" --policy "$env:USERPROFILE\private-export\policy.json" --out "$env:USERPROFILE\cti-ref-index-01" --acknowledge-local-analysis

利用者がインデックスを見て作るTask manifestを使ったbind/verifyは[Task ID紐づけガイド](docs/chatgpt-task-trace-linking-20261120.md)に記載。原文・コマンド・会話タイトルは出力しません。実ユーザーデータを使った互換性・業務完了の独立確認は未検証です。

### Codexの普段のセッションも改善対象にする（Issue #46）

Codex CLIや対応するローカルセッションのrollout-*.jsonlを、明示的に選択してオフラインで分析できる経路を追加しました。Codexのuser_messageとassistant output_textだけを取り込み、ツール引数/実行結果・内部推論・作業ディレクトリ・初期指示は出力しません。既存Session Retrospectiveの7種類の規則へ接続し、AGENTS.md/Skills等の改善候補を確認できます。

    python .\scripts\probe_codex_sessions.py
    python .\scripts\import_codex_rollout_offline.py import --rollout "C:\path\to\rollout-...jsonl" --policy "$env:USERPROFILE\codex-analysis-private\policy.json" --out "$env:USERPROFILE\codex-analysis-private\run-001" --acknowledge-private-local-import

入力元と保存先はGit管理外。**保存ZIPには選別した発言が含まれるため、機密情報として取り扱うこと。** Windows実機の保存先に291件（約158MB）のrolloutが存在することを本文未読で確認しましたが、実セッションのJSON本文による互換性・性能改善は未検証です。使用手順と安全制約は[Codex取込ガイド](docs/codex-rollout-ingest-20261120.md)を参照してください。

### Codex専用Phase B：AGENTS.md / Skillsの未適用差分検査

Codex由来の検証済みsession ZIPを選ぶと、Codex用のAGENTS.md・階層別AGENTS.md・.agents/skills/*/SKILL.mdを対象に、既存のPhase Bで改善提案を検査できます。別製品と混在した入力は拒否します。.codex/config.toml等はSHA棚卸しのみで、モデル・権限・役割TOMLへのdiffは作成しません。

実機の古いrollout1件（204,738バイト、15レコード）の**イベント構造だけをローカルで確認**しました。未知top-level=2件、未知event_msg=3件があり、内容の完全取込・原因分析は未検証です。会話本文・コマンドは出力していません。構造診断の安全なCLI、Phase Bのrequest/grade/verify、利用制限は[Codex Phase Bと構造診断](docs/codex-phase-b-review-20261120.md)を参照してください。

### Codexイベント分析：ターン状態・累積トークン・エラー候補

Codex rolloutから、公式EventMsgのタスク開始/終了、warning/error、stream_error、中断、シェル終了コード、累積トークンusageを**固定カテゴリと数値だけ**で集計できます。最後の累積usageを保持し、履歴中のsnapshotを足し合わせない設計です。エラー候補には最大12件のJSONL元行番号だけを残し、コマンドや会話本文を出力しません。Codexのターン完了は業務完了とは異なり、改善効果や請求額は未検証です。

実PCの古いrollout1件では、以前の未知top-level2件がtoken_usage_record / world_state、未知event_msg3件がtask_started / item_completed×2と分類できました。イベント型の認識が進んでも内部内容は復元していません。詳細は[Codexイベントテレメトリ](docs/codex-event-telemetry-20261120.md)。

### Codex Task Evidence：Task ID・JUnit・GitHub参照の匿名照合

Codex rolloutの固定数値と、手動指定したJUnit XMLテスト報告・GitHub PR番号を、**原本SHAと明示的なTask ID**で束ねるオフライン経路を追加しました。XMLのtestcase数と成否を解析し、GitHub PRは参照形式まで検査します。ただしテスト実行の真正性やGitHubの実マージ状態はオンライン検証していないため、**business_completion_verified=falseを維持**します。

    python .\scripts\review_codex_task_evidence_offline.py index --rollout "C:\path\to\rollout-...jsonl" --test-report "$env:USERPROFILE\codex-private\junit.xml" --policy "$env:USERPROFILE\codex-private\policy.json" --out "$env:USERPROFILE\codex-private\source-index-01" --acknowledge-local-analysis

index → 手動Task ID紐付け → review → 独立manifest SHAのverifyの手順は [Codex Task Evidenceガイド](docs/codex-task-evidence-20261120.md) を参照。原本はGit管理外、外部送信や自動指示変更はなし。

### 自宅用モデルルート（Issue #52）

個人PCで利用する **OpenAI Responses API / Codex CLI / GitHub Copilot CLI** のモデル呼出ルートを追加しました。model-requestのtrack・roleに対応するmodel-planのbackend・model_idを選び、1回ずつ実行します。knowledgeとretrospectiveのgenerator/reviewerをそれぞれ設定できます。生成から評価への自動連続実行は行わず、Retrospectiveの応答JSONは既存Phase Bの検査へ渡せます。

    python .\scripts\probe_personal_model_routes.py
    python .\scripts\run_personal_model_route.py preflight --plan "$env:USERPROFILE\private-llm\model-plan.json" --request "$env:USERPROFILE\private-llm\model-request.json" --policy "$env:USERPROFILE\private-llm\policy.json" --out "$env:USERPROFILE\private-llm\preflight-001"

最初に使う経路の認証設定とモデルIDを用意してください。Codexは保存済み認証またはCODEX_API_KEY等、OpenAI APIはOPENAI_API_KEY、専用の一時設定で動かすCopilotルートは対応するGitHubトークン環境変数を使います。内容確認後、preflightで作ったscope SHAを指定してrunすると1回送信します。実モデル呼出、費用、環境の認証成功は未確認です。Bedrock AgentCoreは従来の別経路です。詳しい初期設定・run/verify/export-jsonは[自宅用モデルルート手順](docs/personal-local-model-routes-20261120.md)を参照してください。

## セッションのローカル準備

```powershell
python .\scripts\prepare_sessions.py --list
python .\scripts\prepare_sessions.py --session "<確認したID>" --out "$env:USERPROFILE\copilot-learning-staging"
```

既定では`~/.copilot/session-state/<session-id>/events.jsonl`を想定。**利用するWindows PCとCopilotの種類/バージョンで**保存形式・終了状態の確認が必要です。最終更新からの経過時間だけでは終了を厳密に保証できません。ZIPの既知パターン検査では、非公開情報・個人情報の完全除去は保証できません。

## 設計・検証資料

- [現在地・残課題](docs/current-status-20261115.md) / [最新全体構成](docs/architecture.md) / [オフライン学習処理](docs/offline-pipeline.md) / [AgentRunとの歴史的比較](docs/agentrun-process-comparison-20261115.md)
- [レビュー修正](docs/review-response-20261114.md) / [知識台帳・検索](docs/offline-improvements-20261115.md)
- [送信前検証・共通評価](docs/preflight-eval-hardening-20261115.md) / [検索品質評価](docs/retrieval-evals-20261115.md)
- [確定バンドル・意味レビュー](docs/frozen-bundle-and-semantic-gate-20261115.md) / [機密検査・適用範囲評価](docs/privacy-semantic-holdout-20261115.md)
- [署名検証・ペア比較](docs/offline-attestation-paired-eval-20261115.md) / [Skillプレビュー](docs/skill-preview-20261115.md)
- [Wiki二方式](docs/wiki-dual-approach-20261115.md) / [OKF対応・移行](docs/okf-wiki-20261115.md) / [Memory設定](docs/agentcore-memory-recommendations-20261115.md)

古い設計文書にあるオンライン手順は現時点では利用できません。データ所有者の許可・送信先/認証の検証・実環境試験（組織利用では必要な承認/DLPも）を完了し、**安全ゲートのコード変更を別レビューするまで**オンライン処理は停止します。Issue #6〜#11は残課題を維持しています。

## 単一RuntimeのCodeZip（Issue #25）

Managed Harnessを使用せず、S3の版付きreadyジョブ→Lambda→1つのRuntimeで処理します。`custom`、`original_wiki`、`custom_and_original_wiki`、`memory_ingest`、`memory_wiki`、`hybrid`を選択できます。既存の学習・引用検査・OKF処理を再利用し、Runtime内でWikiのBedrock呼出経路まで接続しました。実モデル・AWSとの動作は未検証で、既存の外部実行停止を維持しています。

```powershell
python -m pip install -r requirements-runtime.txt
python .\scripts\demo_single_runtime.py --out "$env:USERPROFILE\single-runtime-demo-01"
python .\scripts\build_runtime_zip.py --out "$env:USERPROFILE\copilot-runtime.zip"
python .\main.py --port 18080
```

`/ping`は起動確認、`/invocations`のproduction経路は安全レビュー前のため503になります。ローカル合成デモはテスト専用アダプターを使います。コードがあることを実運用の利用許可・安全性確認とは扱いません。[構成・モード・入力・再開・ZIP配備手順・未完了事項](docs/single-runtime-codezip-20261115.md)を参照してください。

## Memory待ちの有限回自動再開

単一Runtimeと同じ起動Lambdaを維持し、`WAITING_MEMORY`だけを定期点検するコードを追加しました。次回時刻・回数・総待ち時間を台帳で管理し、設定変更や上限超過は`RECOVERY_REQUIRED`で停止します。起動応答が失われても状態を巻き戻さず、既存checkpointを使います。

CloudFormationの`EnableMemoryResume`は既定`false`で、SchedulerもDISABLEDです。AWS実送信停止の解除・デプロイは実施していません。Memoryの抽出完了や実学習品質を認定する機能ではありません。[仕様・制約・検証](docs/memory-resume-20261115.md)
