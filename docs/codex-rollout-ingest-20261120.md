# Codexのrollout履歴をオフラインで分析する（Issue #46）

更新：2026-11-20。Codex CLIや対応するローカルセッションのrollout JSONLを、Copilot Self-Learning Platformの既存Session Retrospectiveへ接続する。

## 根拠と現状

OpenAI公式のCodex実装とテストでは、sessionsディレクトリ内のrollout-*.jsonlにtimestamp/type/payloadが記録され、session_meta、event_msg.user_message、response_item.message等が存在する。

- https://github.com/openai/codex/blob/main/codex-rs/rollout/src/recorder.rs
- https://github.com/openai/codex/blob/main/codex-rs/rollout/src/recorder_tests.rs
- https://github.com/openai/codex/blob/main/codex-rs/app-server/tests/common/rollout.rs

2026-11-20にWindows PC DESKTOP-KM8CL39でメタデータのみ確認：~/.codex/sessionsあり、rollout JSONL 291件、合計158,422,307バイト。Codexコマンドも存在。セッションのファイル名・本文・実行コマンドは読んでいない。

## 取り込む範囲

公式のrecord形式を明示的な許可リストで処理する。Codexのevent_msg.user_message（文字列）とassistantのresponse_item.message内のoutput_textだけを標準events.jsonlへ変換する。ユーザー発言がevent_msgとresponse_itemの両方にある場合はevent_msgを優先し、重複を避ける。

ツール呼び出しはshell/patch/search/MCP/otherの固定カテゴリ別件数のみ出力する。コマンド引数、ツール結果、内部reasoning、cwd、base_instructionsや添付は取り込まない。未知イベントや画像などの文脈不足は件数を記録する。明示的なerror/turn_abortedイベントは異常の兆候であって業務失敗や完了の証明ではない。

## 利用手順（Windows PowerShell）

本文を読まない保存先の匿名確認：

~~~powershell
python .\scripts\probe_codex_sessions.py
~~~

利用者が安全なローカル端末で古いrolloutを選択する例（このコマンドはローカルでファイル名を表示する）：

~~~powershell
Get-ChildItem "$env:USERPROFILE\.codex\sessions" -Filter "rollout-*.jsonl" -Recurse -File |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 10 FullName, LastWriteTime, Length
~~~

実データの利用許可、会社の情報保護規程、専用のGit管理外保管先を確認する。機密語辞書は既存のoffline-sensitive-literals形式でGit管理外に用意する。辞書にはsensitive_literals配列が必要。

手動で選んだ1ファイルだけをローカル取込：

~~~powershell
python .\scripts\import_codex_rollout_offline.py import --rollout "C:\path\to\rollout-....jsonl" --policy "$env:USERPROFILE\codex-analysis-private\policy.json" --out "$env:USERPROFILE\codex-analysis-private\run-001" --acknowledge-private-local-import
~~~

ZIPには選別された会話テキストが入るため、機密扱いで保管する。実コード・実セッション本文・認証情報を公開GitやChatGPTへコピーしない。5分以内に更新されたrolloutは既定で処理を拒否するが、更新からの経過時間だけではセッション終了は保証できない。

出力されるファイル：

- session-(fingerprint).zip：Codex由来のユーザー/assistant発言のみのevents.jsonl
- session-(fingerprint).manifest.json：元のrollout SHA、ZIP SHA、版固定と未承認状態
- codex-analysis.preview.json：ツールカテゴリ件数とSession Retrospectiveの規則ベース改善候補
- codex-import-receipt.json：元ファイル/辞書のSHA、元行番号とイベント番号の対応、成果物SHA（本文なし）

返されたreceipt_sha256を別途保管し、元rolloutと辞書と生成ファイルの同一性を検証：

~~~powershell
python .\scripts\import_codex_rollout_offline.py verify --rollout "C:\path\to\rollout-....jsonl" --policy "$env:USERPROFILE\codex-analysis-private\policy.json" --out "$env:USERPROFILE\codex-analysis-private\run-001" --acknowledge-private-local-import --expected-receipt-sha256 "(保存した64桁SHA)"
~~~

既存のSession Retrospectiveでも同じZIPを分析する：

~~~powershell
python .\scripts\session_retro_offline.py --staging "$env:USERPROFILE\codex-analysis-private\run-001" --fingerprint "(取込結果のsession_fingerprint)" --out "$env:USERPROFILE\codex-analysis-private\retro-001"
~~~

その後、既存のPhase Bのinstruction_review_offline.pyでAGENTS.mdやSkills等の指示版を棚卸しして差分候補を検討できる。ただし自動的なLLM推論・編集はこの取込機能の対象外。

## 制限

- 全Codexクライアント/バージョンでの互換性は未検証。アプリやクラウド側でこのJSONLがないケースは別の入力経路が必要
- 1ファイル48MiB、100,000行、抽出メッセージ8,000件、抽出JSONL合計12MiBの上限あり
- 選別した発言だけを既知の機密パターンとユーザー辞書で検査。未知の機密や業務固有の秘密を完全検出するものではない
- ツール成功・業務完了・コスト・改善効果は計測しない。元セッションへの出典はSHAと行番号で保持
- Git管理外の新規フォルダにだけ出力し、AWS/Bedrock/LLM接続、GitHub Actions/CI、AGENTS.md/Skillsの自動更新はしない

## 合成テスト

~~~powershell
python -m unittest discover -s tests -p test_codex_rollout.py -v
~~~

合成試験でのPASSは実業務の改善効果やセッションの機密保護を保証しない。
