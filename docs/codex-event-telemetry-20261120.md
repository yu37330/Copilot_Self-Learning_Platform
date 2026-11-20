# Codex rollout：公式イベントとテレメトリ分析（Issue #46）

更新：2026-11-20。公式EventMsgのターン開始・終了、明示エラー、警告、コマンド終了コード、累積トークンのスナップショットを、**原文を出さずに集計**する機能を追加。

## 一次資料

OpenAI公式のCodexプロトコルおよび履歴保存実装を確認した。

- https://github.com/openai/codex/blob/main/codex-rs/protocol/src/protocol.rs
- https://github.com/openai/codex/blob/main/codex-rs/history/src/lib.rs
- https://github.com/openai/codex/blob/main/codex-rs/history/src/rollout_payload.rs

turn_started/task_started、turn_complete/task_complete、token_count、error、warning、stream_error、turn_aborted、exec_command_end等は公式に定義されたイベントである。履歴のトップレベルにはtoken_usage_record、world_state、retained_contextなども存在する。今回の型一覧は確認時点の公式mainに基づき、将来の全形式への互換性は保証しない。

## 追加した集計値

| 属性 | 扱い |
|---|---|
| turn_started_count | Codexのターン開始件数。依頼達成の根拠ではない |
| turn_complete_record_count | Codexのターン終了件数。業務の完了ではない |
| turn_complete_with_structured_error_count | 明示的な構造化終了エラー件数 |
| explicit_error_event_count / warning_event_count | エラー・警告を別々に集計 |
| turn_aborted_event_count / stream_error_event_count | 中断やストリームエラーの兆候 |
| exec_command_end_nonzero_count | 数値の非ゼロ終了コードを持つコマンド終了イベント件数 |
| usage_snapshot_count / usage_snapshot_unavailable_count | 利用量観測件数・未観測件数 |
| latest_cumulative_token_usage | 最後に観測した累積のinput/output/その他token件数 |
| cost_usd | null（料金・請求情報がないので推計しない） |
| review_candidates | 固定された確認候補と、最大12件までの元JSONL行番号 |

Codexのtoken_countとtoken_usage_recordは**累積使用量**を含むためスナップショットの足し算をしない。最後の有効な値だけを採用し、未観測の場合はnull。tool非ゼロ終了・ターン中断・warningは業務の失敗率を意味しない。確認候補はheuristic_review_onlyであり、LLMの根本原因判定ではない。

内部推論、コード、コマンド引数、stdout、cwd、個人/組織のツール名、モデル名、セッションIDは出力しない。既知のイベントでも省略した意味的文脈があればpartial扱いとする。UNKNOWNタイプを黙って成功扱いしない。

## 実Windowsでの構造確認（会話本文は未出力）

DESKTOP-KM8CL39で以前確認した古いrollout1件（204,738バイト・15レコード）を、ローカルで既知の型だけ照合した。以前の未知top-level2件は**token_usage_record 1件とworld_state 1件**、未知event_msg3件は**task_started 1件とitem_completed 2件**に分類できた。

ただし、world_state/item_completedの中身は再現せず、原本の会話を使った意味的な失敗分析や実トークンの集計はまだしていない。

構造診断CLI：

~~~powershell
python .\scripts\inspect_codex_rollout_schema.py --rollout "C:\path\to\rollout-....jsonl" --minimum-age-minutes 1440 --acknowledge-local-inspection
~~~

## 利用

既存import_codex_rollout_offline.pyの取込結果（codex-analysis.preview.json）にevent_telemetryが含まれる。実ファイルを処理する際は、機密語辞書、明示的同意、Git管理外の新規保存先、利用者・組織の情報管理規程を満たす必要がある。元JSONL・ZIPは非公開として取り扱う。外部LLM/AWS送信・Codexファイル自動変更・GitHub Actions/CIは追加していない。

## 検証

~~~powershell
python -m unittest discover -s tests -p test_codex_event_telemetry.py -v
python -m unittest discover -s tests -p test_codex_rollout.py -v
~~~

本機能は合成データでの検証が中心で、実Codexの業務品質・完了率・費用改善を実証したものではない。
