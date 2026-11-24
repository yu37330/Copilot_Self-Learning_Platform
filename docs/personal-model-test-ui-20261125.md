# Personal Model Test Lab：簡易ブラウザUI

更新：2026-11-25 / Issue #55。OpenAI API・Codex CLI・GitHub Copilot CLIの実行経路（Issue #52）を、Windows PC上のブラウザでテストするための画面です。外部フロントエンドライブラリ不要。Bedrock AgentCoreの既存経路は変更しません。

## 1. まずは送信なしで起動

リポジトリのルートで次を実行します。

~~~powershell
git pull --ff-only
py -3.12 .\scripts\start_personal_model_ui.py
~~~

ブラウザで http://127.0.0.1:8765/ が開きます。自動で開かない場合は同じPCでURLを開いてください。終了はCtrl+Cです。

画面に用意したものは、モデル実行先の切替、モデルID、Knowledge/Retrospectiveの系統とGenerator/Reviewerの役割、SystemとPromptの入力、環境チェック、preflightのSHA、モック実行、JSON結果の表示・コピーです。

操作の流れは、モデルIDと合成データを入力 → 個人/合成データであることを確認 → 「事前検査を実行」 → 「模擬実行（送信なし）」です。

デフォルトでは**実モデル送信不可**です。モデルIDを入力しても外部には送信しません。模擬実行は固定の合成JSONを返すだけで、LLMの生成・請求・評価実績ではありません。

## 2. 実モデルを試す場合だけ有効化

所属先の規程に従い、Git管理外の既存形式の機密辞書を用意します（UTF-8 BOMなし）。

~~~json
{
  "schema_version": "1.0",
  "record_type": "offline-sensitive-literals",
  "sensitive_literals": ["自分で決めた機密語"]
}
~~~

実モデル実行の起動例：

~~~powershell
py -3.12 .\scripts\start_personal_model_ui.py --allow-live --policy "$env:USERPROFILE\private-llm\policy.json"
~~~

実モデルの起動には必ず明示的なpolicyが必要です。SystemとPromptを目視検査した後、事前検査で固定したSHAを確認し、「このスコープの内容を外部送信する」にチェック、SENDと入力して初めて1回呼び出せます。15分経過した承認スコープや使用済み承認は再利用不可。失敗時も自動再送しません。

- OpenAI API：環境変数 OPENAI_API_KEY、利用可能なmodel IDが必要。Responses APIへstore=false/ツールなしで呼び出す
- Codex CLI：インストールと認証済み環境、利用可能なmodel IDが必要。ephemeral/read-only/一時ディレクトリで非対話実行
- GitHub Copilot CLI：CLI本体と対応GitHubトークン/モデル利用権限が必要。HOMEとツールの使用を制限する

実プロバイダーに送信すると課金・クォータ消費の可能性があります。ChatGPTのアプリ契約とOpenAI API契約は別の場合があります。CLIの制限はすべての機能の隔離を証明するものではありません。

## 3. セキュリティと制約

- 127.0.0.1だけで待受。別PC・スマートフォンからの接続用ではない
- Host/Origin/CSRFチェック、サイズ上限、CSP、キャッシュ無効、アクセスログ非出力
- プロンプトと応答はメモリと開いたブラウザにだけ保持。モデル結果のディスク保存や元のCodex/ChatGPT履歴の自動取得はしない
- UIは個人・合成データ用。会社・顧客・非公開コード、未許可の会話ログは入力しない
- 機密語辞書は既知語の検査であり、完全なDLPでも本人認証でもない
- APIキーの値はUIに入力/表示しない。環境の設定有無は表示するが、認証成功は未検証
- モデル応答は未信頼のJSONとして表示。実業務完了・学習品質・独立評価合格ではない
- Bedrock/AgentCore、CI/GitHub Actions、AGENTS.md/Skills自動適用は変更しない

## 4. 自分でテストするコマンド

~~~powershell
py -3.12 -X utf8 -m unittest discover -s tests -p test_personal_model_webui.py -v
node --check .\copilot_learning\personal_model_assets\app.js
~~~

合成テストは、HTTPの動作、mockでの外部呼出ゼロ、個別承認、二重実行拒否、改ざん/不正入力、XSSを避けたDOM更新、CSRF/Origin/Hostを検証します。実LLM呼出しはテスト対象外です。

### よくある問題

| 症状 | 対処 |
|---|---|
| 画面が開かない | 同じPCで http://127.0.0.1:8765/ を開く。必要なら --port 8766 |
| モデルを選んでも進めない | 実際のモデルID、入力、自己確認チェックを確認 |
| 送信ボタンが無効 | --allow-live と --policy の明示起動、事前検査PASS、SEND確認が必要 |
| 認証状態が不明 | 画面の診断はAPIキー/CLIの設定有無だけ。ログイン成功ではない |
| モデル応答が失敗 | 接続権限・クォータ・モデルIDをローカルで確認。自動再送はなし |

元のCLI経路の詳細は [自宅用モデルルートの手順](personal-local-model-routes-20261120.md) を参照してください。
