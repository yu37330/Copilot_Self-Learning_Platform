# ChatGPT Task Improvement Phase 2：明示Task IDで業務履歴を結び付ける

2026-11-20、Issue #41。ChatGPTの会話とDesktop Commanderの操作を時刻だけで推測結合せず、明示的なTask ID、会話IDのSHAとツール呼び出し番号・原本SHAを使用して匿名集計するオフライン機能。実業務の成功・原因・ユーザー本人の申告の正しさは検証しない。

## 仕組み

1. index：ChatGPT公式データエクスポート内の選択中の会話分岐とDesktop Commanderのtool-history.jsonlをローカル読取。会話IDのハッシュ、通し番号と各ツールレコードのSHAだけを匿名インデックスへ出力する。チャット本文、タイトル、コマンド引数、ツール結果はコピーしない。
2. bind：ユーザーが作成したTask IDと参照ハッシュの対応manifestを検証し、実在する会話・ツール操作だけを結びつけてタスク別の件数・改善候補を表示する。時刻の近接はリンク根拠に使わない。
3. verify：元JSON、JSONL、紐付けmanifest、機密辞書とプレビューを再計算し、独立して控えたmanifest SHAとの一致を確認する。自動編集・外部送信はしない。

ChatGPTの会話IDやDesktopコマンドのハッシュも辞書攻撃の可能性があるため、インデックスは公開Gitに置かない。原本や辞書はローカルのGit管理外に保管すること。

## 使い方（PowerShell）

まずChatGPTの公式エクスポートから、既存のmapping/current_node形式の会話JSONを安全に保存する。Desktop Commanderのローカルtool-history.jsonlか、利用者がAPIから保存したJSON配列も準備する。どちらも組織の取扱許可がある場合のみ分析する。

参照インデックスの作成（既存フォルダの上書き不可）：

~~~powershell
python .\scripts\bind_chatgpt_task_trace_offline.py index --chatgpt-json "$env:USERPROFILE\private-export\conversations.json" --desktop-history "$env:USERPROFILE\private-export\tool-history.jsonl" --policy "$env:USERPROFILE\private-export\policy.json" --out "$env:USERPROFILE\cti-ref-index-01" --acknowledge-local-analysis
~~~

出力 task-trace.index.json の source_sha256 / conversations / desktop_calls を参照し、Git管理外のtask-bindings.jsonを作成する。

~~~json
{
  "schema_version": "1.0",
  "record_type": "explicit-chatgpt-desktop-task-bindings",
  "source_sha256": {"chatgpt": "<インデックスのChatGPT SHA>", "desktop": "<インデックスのDesktop SHA>"},
  "tasks": [
    {
      "task_id": "release-task-001",
      "conversation_ref": "<indexのconversation_ref>",
      "desktop_calls": [
        {"index": 0, "call_sha256": "<indexの呼出0のSHA>"},
        {"index": 1, "call_sha256": "<indexの呼出1のSHA>"}
      ],
      "completion_claim": "not_claimed"
    }
  ]
}
~~~

completion_claim は not_claimed / reported_complete の2択。後者でも業務完了は未検証とする。1つの会話やツール呼び出しを複数のTaskに重複して割り当てる場合は停止する。

明示したTaskだけを匿名集計：

~~~powershell
python .\scripts\bind_chatgpt_task_trace_offline.py bind --chatgpt-json "$env:USERPROFILE\private-export\conversations.json" --desktop-history "$env:USERPROFILE\private-export\tool-history.jsonl" --policy "$env:USERPROFILE\private-export\policy.json" --binding "$env:USERPROFILE\private-export\task-bindings.json" --out "$env:USERPROFILE\cti-task-review-01" --acknowledge-local-analysis
~~~

新規フォルダへtask-trace.review.jsonとmanifest.jsonを出力。Task ID自体を出力せず、ハッシュで識別する。タスク別の会話修正シグナル、ツール別件数、エラーフラグ、改善の候補を提供する。外部GitHub成果物などは取得していないため、completion_verification=not_verified_requires_independent_artifactのまま。

bind時のmanifest_sha256を独立して控え、後から照合：

~~~powershell
python .\scripts\bind_chatgpt_task_trace_offline.py verify --chatgpt-json "$env:USERPROFILE\private-export\conversations.json" --desktop-history "$env:USERPROFILE\private-export\tool-history.jsonl" --policy "$env:USERPROFILE\private-export\policy.json" --binding "$env:USERPROFILE\private-export\task-bindings.json" --out "$env:USERPROFILE\cti-task-review-01" --acknowledge-local-analysis --expected-manifest-sha256 "<bind時のSHA>"
~~~

## 対応範囲と残作業

ChatGPT公式エクスポートの新旧すべての内部スキーマに互換性があるわけではなく、既知のmapping/current_node構造だけを認識する。Desktop Commanderの履歴は件数が有限で、長い出力が省略され得る。原本のハッシュは改変検知に役立つが、タスクの関連性・実行者・業務完了の真正性を保証しない。

次はGitHub PR/Issue、正式Publisher、テスト検収などの外部成果物を独立確認する機能、実データでの版別受入、タスクIDの業務運用、独立LLM評価・holdout実測を開発する。GitHub Actions/CI、AWSオンライン実行、AGENTS.md/Skills自動変更は追加しない。

合成テスト：

~~~powershell
python -m unittest discover -s tests -p test_task_trace_binding.py -v
~~~
