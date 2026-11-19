# Issue #36 — VS Code Chat JSONエクスポートの安全なオフライン取込

更新：2026-11-19。対象はユーザーが明示的に選んだVS Codeの Chat: Export Chat... JSON。既存のCopilot CLI events.jsonlとは別形式として扱う。AWS・LLM・GitHub Actions・自動Skills反映なし。

## 根拠と対応範囲

Microsoft VS Code公式コードのchatImportExport.tsでは、エクスポートがChatModel.toExport()のJSONであることを確認した。chatModel.tsでは、IExportableChatDataのrequests配列、各requestのmessage、response、timestampなどを定義している。公開されている実際のエクスポート例でも message.text と response[].value を確認できた。

一次資料：
- https://github.com/microsoft/vscode/blob/main/src/vs/workbench/contrib/chat/browser/actions/chatImportExport.ts
- https://github.com/microsoft/vscode/blob/main/src/vs/workbench/contrib/chat/common/model/chatModel.ts
- https://code.visualstudio.com/docs/agents/run/sessions/session-history

取り込むのはrequests[].message.text（旧形式なら文字列）と、kindなしMarkdown応答のresponse[].valueだけ。数値epochミリ秒のtimestampを使う。responseTimestampがなければrequest時刻で代用し、その件数・根拠をreceiptに記録する。思考・ツール呼出/結果・hidden/system request・添付・内部コンテキストは本文として取り込まない。未知のpartは未対応イベントに置き換えるため、既存知識評価の安全側判定が維持される。

## 実行方法

まずVS Codeで Chat: Export Chat... を操作し、利用許可のあるセッションだけをGit管理外のローカルディレクトリに保存する。組織利用なら規程・DLP・データ所有者の許可を先に確認する。既存のoffline-sensitive-literals形式の機密語辞書を別のローカルファイルとして用意する。

~~~powershell
python .\scripts\import_vscode_export_offline.py import --export "$env:USERPROFILE\copilot-export-private\chat.json" --policy "$env:USERPROFILE\copilot-export-private\policy.json" --out "$env:USERPROFILE\copilot-export-private\converted-01" --acknowledge-local-import
~~~

出力先は必ずGit管理外の新規フォルダ。session-(fingerprint).zip、session-(fingerprint).manifest.json、import-receipt.jsonの3ファイルが作成される。ZIPには会話の限定本文が含まれるため秘密として保管する。manifestには元のJSONのSHAとZIPのSHA、receiptには出典イベントindex/リクエストindex、除外理由、時刻代用件数、ポリシーSHAだけを保存する。元のexport JSON全文はコピーしない。

CLIが返すreceipt_sha256を別途保管し、再検証する。

~~~powershell
python .\scripts\import_vscode_export_offline.py verify --export "$env:USERPROFILE\copilot-export-private\chat.json" --policy "$env:USERPROFILE\copilot-export-private\policy.json" --out "$env:USERPROFILE\copilot-export-private\converted-01" --acknowledge-local-import --expected-receipt-sha256 "(最初のreceipt SHA)"
~~~

既存のPhase Aレビュー機能へは次のように接続する。

~~~powershell
python .\scripts\session_retro_offline.py --staging "$env:USERPROFILE\copilot-export-private\converted-01" --fingerprint "(変換結果のsession_fingerprint)" --out "$env:USERPROFILE\copilot-export-private\retro-preview-01"
~~~

## データ保護

- 原本全体で既知の認証情報/メール/電話/JWTとローカル機密語辞書を検査してから変換
- 日本語に連結されたメールアドレス・APIキー風文字列も追加パターンで検出
- 大きさ、読み取り中の変更、壊れたJSON、元の形式違い、hidden/system、欠落時刻・本文を安全側で拒否/保留
- Git管理ディレクトリ・自動発見ディレクトリ・既存出力の上書きを拒否
- ソースファイル・ポリシー・成果物の変更は独立保管receipt SHA再検証で拒否
- 既知パターンによる検査は機密保護を保証せず、会社の承認や専門DLPを置き換えない

## 検証・未完了

合成JSONの変換→既存ZIP/manifest検査→Phase A正規化の往復をユニットテストで確認する。まだ本人環境のVS Codeで生成した実JSONのテストではない。Export Chat as Zip全体、ツール結果/思考の解釈、時刻がないセッション、異なるベンダーの形式、実LLM評価、実採用/自動配布は未実装。Issue #36は実機受入が終わるまでOPENを維持する。

~~~powershell
python -m unittest discover -s tests -p "test_vscode_export_adapter.py" -v
~~~
