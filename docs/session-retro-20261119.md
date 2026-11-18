# セッション振り返りと指示改善プレビュー（Issue #31）

実装日: 2026-11-19。関連: #2 / #6 / #7 / #8 / #10 / #11 / #25。

## 目的と設計

既存のCopilotセッションから、ユーザーによる訂正・繰り返し指示・ツール失敗といった兆候をオフラインで検出する。知識を再利用する従来のSkillプレビューとは別に、指示ファイルやSkills、Agents、依頼文、テストのどこを見直すとよいかの候補を提案する。

GitHub Copilot CLIのChronicle improveに近い目的だが、Chronicleや実LLMを呼び出す実装ではない。保存形式は現行のWindows Copilotで実機確認できていないため、既存の検証済みarchive・normalized-sessionだけを入力にする。

## 初期版でできること

- 最大32セッション・合計15000メッセージを対象とする
- 既存のread_archiveとnormalize_eventsでZIP/manifest/SHA/出典IDを検査
- 指示違反、完了検証漏れ、繰り返し手順、役割境界、局所指示、訂正、ツール失敗の7種類を規則で検出
- 出典fingerprint、archive SHA、evidence_id、件数、セッション数、coverageを記録。会話原文は出力しない
- 検討先を copilot-instructions.md / AGENTS.md / *.instructions.md / SKILL.md / *.agent.md / prompt / test_or_skill に分類
- 固定文言の改善候補と検証案を提示。根本原因、既存指示との矛盾、改善効果は未確認と明記
- Git管理外の新規フォルダだけにプレビューを出力し、独立保管したmanifestハッシュで再検証する

未知イベントがある場合はcoverage=partial_unrecognized_events。検出されなかった場合も「問題なし」を意味しない。エラーや訂正発言は誤検出があり、原本での人手確認が必須。

## Windows PowerShellでの使用法

まず対応するCopilotの保存形式とデータ利用許可を確認し、既存収集器でZIPを作る。

~~~powershell
python .\scripts\prepare_sessions.py --list
python .\scripts\prepare_sessions.py --session "<セッションID>" --out "$env:USERPROFILE\copilot-learning-staging"
~~~

表示された64桁のfingerprintを指定する。複数指定すると横断分析する。

~~~powershell
python .\scripts\session_retro_offline.py --staging "$env:USERPROFILE\copilot-learning-staging" --fingerprint "<fingerprint-1>" --fingerprint "<fingerprint-2>" --out "$env:USERPROFILE\copilot-retro-preview-01"
~~~

生成ファイルはretrospective.preview.json、RETROSPECTIVE.preview.md、manifest.jsonの3件。AGENTS.mdやSKILL.mdなど自動読込用のファイル名にはしない。

CLIの出力にあるmanifest_sha256をプレビューとは別の場所に保管し、同一入力で検証する。

~~~powershell
python .\scripts\session_retro_offline.py --staging "$env:USERPROFILE\copilot-learning-staging" --fingerprint "<fingerprint-1>" --fingerprint "<fingerprint-2>" --out "$env:USERPROFILE\copilot-retro-preview-01" --verify --expected-manifest-sha256 "<export時のmanifest_sha256>"
~~~

## 安全境界

既存のCopilot設定、Skills、Agents、台帳は一切変更しない。Git管理内や自動読込用ディレクトリへの出力は禁止。本文のプレビュー転載、モデル実行、Memory投入、ネットワーク通信、S3送信、CI/GitHub Actionsは追加しない。require_online_release()によるオンライン停止は維持する。

実セッションの原本やプレビューはローカルで保護し、公開GitHubへ追加しないこと。既知パターンによる機密検査は社内DLPや人間による判断の代わりにならない。

## 今後の段階

Phase B（未実装）は、独立Gradeと人手レビューを使い、LLMによる文脈の解釈、既存ファイルとの重複・競合確認、差分形式の変更提案を作る。外部送信の許可とプロンプトインジェクション対策が必要。

Phase C（未実装）は、指示の変更前後をholdoutタスクで比較し、成功率、手戻り数、ツール呼び出し数、トークン、コスト、悪化率を測定する。実測がない指標はnullとし、改善達成を主張しない。

## 合成回帰テスト

~~~powershell
python -m unittest discover -s tests -p "test_session_retro.py" -v
~~~

対象は役割別の検出、出典改変、raw非出力、安全な保存先、再検証、ZIPからのCLI往復、ネットワーク・subprocess呼出しゼロ。WindowsとLinuxのテスト結果は別々に記録する。実Copilotでの適合性と改善効果は未検証。
