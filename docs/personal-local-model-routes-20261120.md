# 個人PC用モデル実行ルート：OpenAI API / Codex CLI / GitHub Copilot CLI

Issue #52、2026-11-20。Copilot Self-Learning Platformの既存Knowledge Learning（知識抽出）とSession Retrospective（Phase B指示改善）に、**Bedrock AgentCoreとは別の個人用モデル呼出経路**を追加する。AWS側のreview_safety.require_online_releaseは一切変更しない。

## 使い分け

| backend | 実行手段 | 主な条件 |
|---|---|---|
| openai_api | 公式OpenAI Responses API、store=false、tools指定なし | OPENAI_API_KEYが必要。API利用はChatGPT契約と別課金の場合がある |
| codex_cli | codex exec --ephemeral --sandbox read-only --ignore-user-config --model | 対応Codex CLIと認証が必要。プロジェクト設定を無視し空の一時作業ディレクトリで実行 |
| copilot_cli | copilot、stdin入力、-s、--no-ask-user、--model、--deny-tool=shell,write,read,url,memory | 対応GitHub Copilot CLIと認証が必要。専用の一時COPILOT_HOMEを使用 |
| bedrock / AgentCore | 従来のAWSルート | **今回未変更、外部実行安全ゲート停止を維持** |

※ Codex CLIのread-onlyは読み取り専用の意味で、ファイル閲覧も原理上あり得る。Copilotの拒否ルールや一時HOMEもすべてのプラグイン/認証情報へのアクセスを証明するものではない。職場のPC・企業情報を入力しないことが前提。

OpenAI公式資料：
- https://developers.openai.com/api/docs/guides/migrate-to-responses
- https://learn.chatgpt.com/docs/non-interactive-mode
GitHub公式資料：
- https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-programmatic-reference
- https://docs.github.com/en/copilot/how-tos/copilot-cli/automate-copilot-cli/run-cli-programmatically

2026-11-20にWindows DESKTOP-KM8CL39で環境変数の**有無だけ**を確認：Codex CLIは存在し、execのephemeral/read-only/ignore-user-configフラグを確認。Copilot CLIはPATH上になく、OPENAI_API_KEYも未設定。**実課金を発生させるモデル呼び出しはしていない**。使うモデルIDやCLIのバージョン・エンタイトルメントは個人PC側で確認する必要がある。

## まず環境を確認

~~~powershell
python .\scripts\probe_personal_model_routes.py
~~~

この診断はコマンドの存在とOpenAI APIキーの設定有無だけを表示し、鍵の内容・個人名・パスやモデルリクエストは表示しない。ネットワーク接続・認証確認をしない。

## 個人/合成の入力を明示的に準備

Git管理外のprivateなフォルダを用意し、次の合成サンプルをコピーして、**実際に契約上利用可能なモデルIDに書き換える**。

~~~powershell
Copy-Item .\examples\personal_local_model_plan.synthetic.json "$env:USERPROFILE\private-llm\model-plan.json"
Copy-Item .\examples\personal_local_model_request.synthetic.json "$env:USERPROFILE\private-llm\model-request.json"
~~~

※ private-llmフォルダは先に作成する。モデル計画の2系統（knowledgeとretrospective）は、generatorとreviewerで異なるmodel_idを指定する必要がある。provider_familyは自己申告にすぎず、独立評価モデルの実提供元を検証したことにはならない。

Windows PowerShell 5.1で Set-Content -Encoding UTF8 を使ってpolicy.json等を作ると、UTF-8のBOMが先頭に付く場合がある。既存の機密辞書検査はBOM付きJSONを拒否する。UTF-8 **BOMなし**で保存すること。たとえば既存ファイルをBOMなしで保存し直す場合、次のローカル操作を利用できる。

~~~powershell
$path = "$env:USERPROFILE\private-llm\policy.json"
$body = [System.IO.File]::ReadAllText($path)
[System.IO.File]::WriteAllText($path, $body, (New-Object System.Text.UTF8Encoding($false)))
~~~

初期の合成preflight実機確認でも、BOM付きpolicy.jsonでは送信前に拒否され、BOMなしに保存し直すと preflight_only_no_model_call で成功した（backend=codex_cli、role=generator、prompt_bytes=276、network_calls=0）。認証・モデル実行は行っていない。

入力JSONの必須項目：
- track は knowledge または retrospective
- role は generator または reviewer
- system はモデルへのタスク指示、prompt は実際に送る本文
- source_classification は personal_or_synthetic_only
- manual_content_reviewed は自分で原文を検査済みの場合だけtrueにする

**職場/会社/顧客の会話・コード・業務ログは、このルートに直接送ってはいけない**。一般公開済みであっても、職場の契約・利用規程によっては事前承認が必要。静的機密語辞書による検査は完全なDLPではない。

既存Phase Bで生成した資料を使う場合は、まずGit管理外にあるreviewer-request.preview.jsonから**未検査の下書き**を作成できる。

~~~powershell
python .\scripts\prepare_personal_model_request.py --artifact "$env:USERPROFILE\private-llm\phase-b-reviewer-request.preview.json" --role generator --out "$env:USERPROFILE\private-llm\review-request-draft.json" --acknowledge-local-copy
~~~

生成した下書きはsource_classification=UNREVIEWED_REQUIRES_HUMAN_CLASSIFICATION、manual_content_reviewed=falseなので**そのままではpreflightできない**。個人/合成データだけが含まれることを実際に確認して、利用可否を判定し、本人が2項目を編集する必要がある。別モデルによるReviewerに渡す場合は独立Reviewer用のoffline-retro-independent-review-requestの下書きをrole=reviewerで作る。抽出・評価結果には、さらに既存の独立Gradeと安全ゲートが必要。

## 事前検査 → 明示承認 → 実行

実行には、Git管理外の機密語辞書policy.jsonを用意する。辞書は既存offline-sensitive-literals形式。

1. preflightを先に実行。これは**API/CLIを呼ばない**。送信本文、model ID、backend、policyのSHAを固定する。

~~~powershell
python .\scripts\run_personal_model_route.py preflight --plan "$env:USERPROFILE\private-llm\model-plan.json" --request "$env:USERPROFILE\private-llm\model-request.json" --policy "$env:USERPROFILE\private-llm\policy.json" --out "$env:USERPROFILE\private-llm\preflight-001"
~~~

返されたscope_sha256を別途控える。生成されたscope.preflight.jsonには本文を含めず、backend/model_id/role/入力サイズ/ハッシュだけを保持する。**送信内容は必ず元のmodel-request.jsonを開いて確認する**。scope SHAは本人の電子署名や業務利用許可を証明するものではない。

2. 送信先、モデル、本文の扱いを本人が確認した上でのみrun。下のSCOPE_SHAをpreflightの出力に置き換える。

~~~powershell
python .\scripts\run_personal_model_route.py run --plan "$env:USERPROFILE\private-llm\model-plan.json" --request "$env:USERPROFILE\private-llm\model-request.json" --policy "$env:USERPROFILE\private-llm\policy.json" --preflight "$env:USERPROFILE\private-llm\preflight-001" --approved-scope-sha256 "SCOPE_SHA" --acknowledge-external-send --out "$env:USERPROFILE\private-llm\model-result-001"
~~~

この操作のみ外部送信を実行する（送信先は選択したopenai_apiまたは認証済みCLIのプロバイダ）。外部モデル実行は料金・クォータを消費する可能性がある。自動リトライはせず、失敗理由/CLI標準エラーをそのまま表示しない。OpenAI APIではstore=falseを送るが、**ゼロ保管・ゼロログ・組織DLPの保証ではない**。

3. runで返るmanifest_sha256を独立に控え、verifyで原本と出力を確認。verifyは**モデルを再実行しない**。

~~~powershell
python .\scripts\run_personal_model_route.py verify --plan "$env:USERPROFILE\private-llm\model-plan.json" --request "$env:USERPROFILE\private-llm\model-request.json" --policy "$env:USERPROFILE\private-llm\policy.json" --preflight "$env:USERPROFILE\private-llm\preflight-001" --expected-manifest-sha256 "MANIFEST_SHA" --out "$env:USERPROFILE\private-llm\model-result-001"
~~~

4. 既存Phase Bの提案検査へ流す場合、検証後にJSON応答をGit管理外の新規ファイルとして抽出する。

~~~powershell
python .\scripts\run_personal_model_route.py export-json --plan "$env:USERPROFILE\private-llm\model-plan.json" --request "$env:USERPROFILE\private-llm\model-request.json" --policy "$env:USERPROFILE\private-llm\policy.json" --preflight "$env:USERPROFILE\private-llm\preflight-001" --expected-manifest-sha256 "MANIFEST_SHA" --out "$env:USERPROFILE\private-llm\model-result-001" --output-json "$env:USERPROFILE\private-llm\untrusted-proposals.json"
~~~

このJSONは既存Phase Bのinstruction_review_offline.pyの--proposalなどへ渡せるが、**モデルの回答構造が既存契約に適合することは保証されない**。そのため既存のgrade_response/独立reviewer/最終確認へ通し、証拠不足・危険な差分は保留する。モデルのsupport判断だけで指示ファイルを自動更新しない。

## 設計上の残件

- 実OpenAI APIキー・有効なCopilot CLI・モデルの利用可能性・請求単価を検証していない。実LLM呼出しの成功と性能は未知
- Codex CLIのread-only/ignore-user-configとCopilotのdeny-toolは権限削減策だが、すべてのモデル行動や認証基盤を完全に制御する保証ではない
- ユーザー本人が個人/合成かどうかを分類するゲートは自己申告。組織の独立したDLP審査の代わりにはならない
- 生成モデルと評価モデルの実独立性はモデルID・provider_familyの自己申告だけでは証明できない
- 依頼の契約はJSONオブジェクトを返すことまで。内容の正しさ・独立検収・Phase Cの改善効果は未実証
- Bedrock AgentCoreの公式Runtime、外部通信停止、GitHub Actions/CI不使用、自動AGENTS.md/Skills配布禁止はそのまま維持する

## テスト

~~~powershell
python -m unittest discover -s tests -p test_local_llm_router.py -v
~~~

全テストは合成データとAPI/CLIのモックによる検証であり、実環境での課金付き実行は行わない。
