# Issue #36：Copilot CLIネイティブセッションの安全なローカル取り込み

更新：2026-11-19。対象は**明示的に選択したローカルのCopilot CLIセッション**。Phase A〜Cの既存オフライン評価・改善提案フローにつなぐ。実AWS/Copilot/LLMの実行、外部送信、自動Skills変更は行わない。

## 形式を確認した根拠

GitHub公式のCopilot CLI設定ディレクトリ資料には、session-state/<session-id>/events.jsonl と workspace.yaml の保存が記載されている。Microsoft VS CodeリポジトリのCopilot CLI統合実装でも、同じ場所をSDKの永続化先として記載している。ネイティブJSONLでは type と data のenvelopeに user.message、assistant.message、tool.execution_complete 等を使用する。

- https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference
- https://github.com/microsoft/vscode/blob/main/extensions/copilot/src/extension/chatSessions/copilotcli/AGENTS.md
- https://code.visualstudio.com/docs/agents/run/sessions/session-history

**注意：Copilot CLIとVS CodeのChat: Export Chat...（手動JSONエクスポート）は別形式。** VS Codeのエクスポート機能は公式に確認済みだが、対象バージョンのJSON内部構造を実機検証していないため、推測で変換しない。probe_session_sources.pyは存在件数を示すだけでエクスポートを解析しない。

## 実装内容

copilot_learning/cli_session_adapter.py を追加し、core.normalize_events()からネイティブ形式を検出したときだけ委譲する。旧方式 type=message の正規化は従来どおり利用可能。

- user.message.data.content の文字列のみを USER として扱う
- assistant.message.data.content の文字列のみを ASSISTANT として扱う
- tool.execution_complete.data.success=false は TOOL の失敗兆候として**固定のエラー文字列**に置換。実ツール引数・出力・エラー詳細を学習用の本文へ転載しない
- tool.execution_complete.data.success=true と既知テレメトリーは無視した件数を記録
- 未知イベント、空の応答、長すぎる内容、成功/失敗不明のツール結果は skipped_details と skipped_event_count に記録し、正しく取り込めたと誤認しない
- timestamp はタイムゾーン付きISO 8601を必須とし、UTCへ正規化。日時を推測しない
- イベント由来の evidence_id（session_fingerprint:event_index）と元のarchive_sha256を維持
- 既知の認証情報、メール、電話、JWTパターンを検出した場合、本文を表示せず拒否
- 混在した旧形式/ネイティブ形式、壊れたdata、ユーザー発言がないセッションは保留

skipped_event_countがある場合、既存の学習評価器は「全履歴確認済み」として昇格できない。ツールの成功メッセージは内容を出力しないため、**リトロスペクティブから全ての成功・失敗理由を復元できるわけではない**。

## メタデータだけの所在確認

まず、会話本文を読まずに保存先の有無と件数だけ確認する。

~~~powershell
python .\scripts\probe_session_sources.py
~~~

--cli-rootで特定の保存先を指定できる。--vscode-export-dirは手動エクスポートしたJSONファイルの件数だけ確認する。**フォルダ・ファイル名や会話本文は標準出力しない。**

既存の収集器も同じ保存先を想定するが、収集対象とバージョンを実環境で確認すること。

~~~powershell
python .\scripts\prepare_sessions.py --list
python .\scripts\prepare_sessions.py --session "<確認したsession-id>" --out "$env:USERPROFILE\copilot-learning-staging"
~~~

prepare_sessions.pyで作られたmanifestにある64桁のsession_fingerprintを利用して、Phase Aのプレビューを作る。

~~~powershell
python .\scripts\session_retro_offline.py --staging "$env:USERPROFILE\copilot-learning-staging" --fingerprint "<64桁fingerprint>" --out "$env:USERPROFILE\retro-native-cli-preview-01"
~~~

全操作はローカルで、出力はGit管理外の**新規フォルダ**のみ。実セッションの利用権限、対象機密、組織ポリシー/DLPを事前確認する。元ZIPは未公開であり、出力に機密が全く含まれない保証はしない。外部送信は既存 require_online_release() で引き続き停止する。

## Windows実機に関して確認できた範囲

DESKTOP-KM8CL39ではCopilot CLIコマンドが見つからず、~/.copilot/session-state も存在しなかった（2026-11-19のメタデータ確認）。一方、VS CodeのworkspaceStorageディレクトリの存在は確認できた。個別の会話/トランスクリプトは開いていない。このため **実CopilotイベントによるE2E検証は未実施**。

## テストと残作業

~~~powershell
python -m unittest discover -s tests -p "test_cli_session_adapter.py" -v
python -m unittest discover -s tests -p "test_session_retro.py" -v
~~~

テストは合成JSONLで、正しいイベント、未知イベント、出典ID、tool失敗の定型化、秘密・PII拒否、混合形式拒否、ZIP→Phase Aプレビュー、所在診断の本文未読を検証。成功しても実Copilotに対する互換性は断定しない。

残る課題：VS Code手動エクスポートの実バージョン別構造と安全なアダプター、CLI実セッションの実機動作確認、社内承認、実験runnerの根拠取得と独立検証。GitHub Actions/CIは追加しない。
