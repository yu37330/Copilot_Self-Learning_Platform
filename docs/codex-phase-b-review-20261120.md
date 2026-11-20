# Codex Phase B：AGENTS/Skills差分レビューと実rolloutの構造確認

2026-11-20、Issue #46。Codex rolloutから、既存のSession Retrospective Phase Aを通してPhase Bの未適用diffを作る経路を実装。既存のCopilot向けファイルとCodex向けの指示を混同しない。

## Windowsで確認した実rolloutの状態

DESKTOP-KM8CL39のローカルrolloutから古い1件を選び、読み取った情報をJSON構造の静的カテゴリだけに限定した。**本文・コマンド・ファイル名・モデル名はツール結果へ出していない**。ソースは204,738バイト／非空15レコード。session_meta=1、response_item=8、event_msg=3、turn_context=1、未知のtop-level=2。event_msg内の未分類タイプ=3、message本文partの型はinput_text=9、output_text=1。response_item.messageの役割別件数はuser=2、assistant=1、developer=3であり、developerは本文取込対象に含まない。

よって、Codexと互換の大枠を確認できたが、未知イベントが残るので**そのファイルの完全取込・意味的な失敗分析・機密安全性を保証しない**。実内容のPhase A/Phase B実行は未実施。

16MiB以下、最低24時間更新されていない1ファイルの構造だけを診断するCLI：

~~~powershell
python .\scripts\inspect_codex_rollout_schema.py --rollout "C:\path\to\rollout-....jsonl" --minimum-age-minutes 1440 --acknowledge-local-inspection
~~~

実際の入力内容はローカルメモリでJSONに読み込むが、出力ファイル・標準出力は固定のイベントカテゴリと件数のみ。企業データを扱う場合は組織の利用承認を得ること。

## Codex用の正式Phase B

- Codex由来のsource_formatがcodex-rollout-jsonl-v1の場合のみCodex用の指示パスへ切替。Copilotとの混在は拒否する。
- Codexの指示一覧：ルートAGENTS.md・AGENTS.override.md、深さ3までの階層別AGENTS.md、プロジェクトの.agents/skills/*/SKILL.md。既存の.codex/config.tomlと.codex/agents/*.tomlは**読取専用のSHA棚卸し**に限定する。
- AGENTS.mdの既存追記、階層別AGENTS.mdの限定追記と新規案、最小frontmatter付きのSkill案は、証拠IDと指示ファイルのSHAを検証してレビュー用diffとして生成できる。**ファイルは変更しない**。
- TOMLロール設定・モデルや権限・エージェント実行設定は差分対象にしない。role_boundaryは人手で検討するprompt_adviceだけ。
- クロス製品の出典混在、未知パス、権限拡大表現、改ざん、ファイル版不一致は拒否。

公式参照：
- AGENTS.md階層：https://developers.openai.com/cookbook/examples/gpt-5/codex_prompting_guide
- Skillsディレクトリ：https://developers.openai.com/api/reference/cli
- Custom agent role TOML：https://developers.openai.com/ja-JP/docs/config-file/config-reference

## 操作手順（合成テストとは別、実履歴の利用は承認前提）

まず既存import_codex_rollout_offline.pyで生成された、利用許可済みrollout由来のsession ZIPとmanifestがGit管理外のフォルダにあることを確認する。詳しくは docs/codex-rollout-ingest-20261120.md。

Phase Bの根拠と現行の指示版の固定：

~~~powershell
python .\scripts\instruction_review_offline.py request --staging "$env:USERPROFILE\codex-analysis-private\run-001" --fingerprint "(64桁fingerprint)" --repo "C:\path\to\repository" --policy "$env:USERPROFILE\codex-analysis-private\policy.json" --out "$env:USERPROFILE\codex-analysis-private\phase-b-request-01"
~~~

別途、利用が許可された環境で人間またはモデルが作ったoffline-proposals.jsonを検査して未適用diffへ変換：

~~~powershell
python .\scripts\instruction_review_offline.py grade --staging "$env:USERPROFILE\codex-analysis-private\run-001" --fingerprint "(同じfingerprint)" --repo "C:\path\to\repository" --policy "$env:USERPROFILE\codex-analysis-private\policy.json" --proposal "$env:USERPROFILE\codex-analysis-private\offline-proposals.json" --out "$env:USERPROFILE\codex-analysis-private\phase-b-grade-01"
~~~

出力時のmanifest SHAを独立保存し、verifyでも原本/指示版/差分が変化していないことを確認する。LLMの自動実行・正式承認・Skillsの自動配布はなし。

## 確認と未完了

合成データのCodex ZIP → read_archive → normalize_events → request → grade → verifyを検証。独立モデルのroot cause分析、実Codexの会話本文での互換・機密・品質確認、LLM利用権限、完成したTaskの成功率・費用評価は未完了。GitHub Actions/CI・AWS通信・自動指示適用の安全停止は維持する。

~~~powershell
python -m unittest discover -s tests -p test_codex_phase_b_review.py -v
~~~
