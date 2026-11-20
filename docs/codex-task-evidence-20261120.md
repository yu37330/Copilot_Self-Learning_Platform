# Codex Task Evidence：タスクID・テスト報告・GitHub参照の証拠ゲート

2026-11-20 / Issue #46・#41。Codexのローカルrollout JSONL、JUnit XMLのテスト報告、GitHub PR参照を、ユーザーが指定したTask IDのもとに固定する**オフライン分析器**。どれかが揃っていても、本人の業務成功や品質改善が実証されたとは判定しない。

## できることと限界

| 出典 | 現在できる検査 | 完了に必要な追加検査 |
|---|---|---|
| Codex rollout | 原本SHA、イベント型、使用量の累積スナップショット、異常候補の元行番号 | CodexがそのTask IDを実際に実行したか、成果物は何か |
| JUnit XML | 原本SHA、testcase/failure/error/skippedの件数、XML内の矛盾 | テストが実際に実行されたか、対象コードの版と環境 |
| GitHub PR参照 | repositoryとPR番号の形式・SHAを検査 | GitHubを別経路から再取得し、PRのmerge/HEADと成果物を照合 |
| Task ID | 利用者が作る明示的な紐付けmanifestとSHAの整合性 | タスクの受入条件と独立QA、改善効果の実測 |

GitHub PRのマージは業務の完了を意味しない。JUnitの成功文字列は、実際のテスト実行の真正性を保証しない。全ソースがハッシュ一致しても business_completion_verified=false とする。

## データ保護

- JSONL、JUnit XML、Task ID対応manifest、レポート、機密語辞書はGit管理外のプライベートなWindowsフォルダで扱う
- コマンド・テスト名・会話本文・失敗ログ・PR名・実セッションIDは結果に表示/保存しない。固定カテゴリと数値のみを表示する
- 最大16MiBで最終更新24時間以上前のCodex rollout1件、最大2MiBのJUnit XML1件が対象。DTD/Entityは拒否。全Codex形式・JUnit形式への互換性は未検証
- 静的な機密語辞書は、全文の意味的機密検査や組織DLPの代わりにはならない。会社データは所属先の取扱規程を確認
- GitHub Actions/CIや外部LLM/AWSへの送信、AGENTS.mdとSkillsの自動変更は行わない

## Windows PowerShellで実行する手順

1. Codex rollout、JUnit XML、既存offline-sensitive-literals形式のpolicy.jsonをGit管理外に準備する。テストXMLは実際のテスト環境から生成したものを選び、機密を含み得るので専用保管する。

2. 明示的なファイルを選び、匿名indexを作る。

~~~powershell
python .\scripts\review_codex_task_evidence_offline.py index --rollout "C:\path\to\rollout-....jsonl" --test-report "$env:USERPROFILE\codex-private\junit.xml" --policy "$env:USERPROFILE\codex-private\policy.json" --out "$env:USERPROFILE\codex-private\source-index-01" --acknowledge-local-analysis
~~~

保存された codex-task-sources.index.json の rollout_sha256 と test_report_sha256 を使って、ユーザーが codex-task-evidence-binding.json を別のGit管理外のフォルダに作成する。

~~~json
{
  "schema_version": "1.0",
  "record_type": "offline-codex-task-evidence-binding",
  "task_id": "codex-task-001",
  "rollout_sha256": "<indexのrollout_sha256>",
  "test_report_sha256": "<indexのtest_report_sha256>",
  "github": {
    "repository_full_name": "owner/repository",
    "pr_number": 123
  },
  "completion_claim": "not_claimed"
}
~~~

JUnitがない場合はコマンドの --test-report を省略し、bindingのtest_report_sha256はnull。PRが存在しない場合はgithub:nullとする。completion_claimはnot_claimedとreported_completeだけで、後者でも完了にはならない。

3. SHAを照合して、Task ID付きの未承認レポートを作る。

~~~powershell
python .\scripts\review_codex_task_evidence_offline.py review --rollout "C:\path\to\rollout-....jsonl" --test-report "$env:USERPROFILE\codex-private\junit.xml" --policy "$env:USERPROFILE\codex-private\policy.json" --binding "$env:USERPROFILE\codex-private\codex-task-evidence-binding.json" --out "$env:USERPROFILE\codex-private\task-review-01" --acknowledge-local-analysis
~~~

結果は codex-task-evidence.preview.json / manifest.json の2ファイル。reportは生のTask IDやGitHub repository名も含まず、それぞれSHAで識別する。review時に返るmanifest_sha256を別の場所に保管する。

4. verifyで元JSONL・XML・機密語辞書・bindingとレポートを再計算する。

~~~powershell
python .\scripts\review_codex_task_evidence_offline.py verify --rollout "C:\path\to\rollout-....jsonl" --test-report "$env:USERPROFILE\codex-private\junit.xml" --policy "$env:USERPROFILE\codex-private\policy.json" --binding "$env:USERPROFILE\codex-private\codex-task-evidence-binding.json" --out "$env:USERPROFILE\codex-private\task-review-01" --acknowledge-local-analysis --expected-manifest-sha256 "<review時のmanifest SHA>"
~~~

verifyの成功は入力とバイト列の一致であり、PRの現在の状態、テスト実行者、実業務の完了・改善効果を保証しない。

## 続きに必要な機能

GitHub接続ツールによる**独立したPRの再取得**と、ローカルTest Runnerからの実行証拠の取得が必要。さらに成果物受入条件が合格した場合だけ、既存Phase Cの未学習タスク比較（成功率・手戻り・トークン・費用）へ入力する。この段階の実装では、PRの公開APIや外部LLMを直接呼ぶことはしない。

## 合成テスト

~~~powershell
python -m unittest discover -s tests -p test_codex_task_evidence.py -v
~~~

合成テスト成功は実ユーザーセッションの効果改善・組織DLP完了・証拠の真正性を示すものではない。
