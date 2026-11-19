# Issue #31｜知識学習系・Session Retrospectiveの2系統・独立LLM評価

更新日：2026-11-20。目的は、候補を生成したモデルが自分の出力を自己評価しないようにすること。オフライン契約、モデルの識別、持込み回答の検査を追加。**実LLM/APIは呼び出さない。**

## 変更前の問題と解消点

- 既存の知識学習CLIは生成と独立Gradeに同じproviderを渡していた。修正後は別アダプターのgrade_providerと別model_idを要求する
- 単一Runtimeではextract_model_idとgrade_model_idを別に指定できたが、同一IDを禁止していなかった。修正後は同じmodel_idを拒否（custom / custom_and_original_wikiモード）
- Session Retrospectiveには別LLMの評価判断を受ける経路がなかった。Phase Bの根拠・指示SHA・機密・危険指示検査を通った提案だけ、独立Reviewerに提示できるようにした
- Reviewerがsupport_for_manual_reviewと回答しても、採用・公開はしない。対応は未承認のまま人手検収に残す

## 2系統のモデル

| 系統 | Generator | Reviewer | 未検証 |
|---|---|---|---|
| 知識学習 | Note / Skill候補の生成モデル | 出典・反証・一般化を点検する別モデル | 実LLM精度 |
| Session Retrospective | 失敗兆候から改善案を考える別途の生成モデル | Phase Bの改善案とテスト計画を再点検する別モデル | 実モデル自動推論と原因の意味評価 |
| Hybrid（参考） | AgentCore Memoryのサービス管理モデル | 独立Grade用のモデルID | Memory内部モデルとの独立性は証明できない |

モデルは各trackのgenerator/reviewerでbackend、provider_family、model_id、max_input_tokensを設定する。現時点で**Claude、Nova、GPTなどの特定バージョンを固定していない**。使用できるモデルID、AWSリージョン、API料金、情報保護は後から事実確認する。

require_cross_vendor=trueなら異なるprovider_familyラベルが必要。ただしラベルは設定作成者の申告であり、提供元やモデルの真正性を検証する機能ではない。backend=bedrock/openai_api/other_offlineは将来の接続先を示す設計値で、OpenAI APIの実装や外部送信の許可を意味しない。

## オフライン実行（Windows PowerShell）

最初に合成IDだけのモデル設定を検査する。

~~~powershell
python .\scripts\dual_model_review_offline.py plan --model-plan .\examples\dual_track_models.synthetic.json --out "$env:USERPROFILE\dual-review-plan-01"
~~~

Session Retrospectiveの既存Phase Bで改善案の生成依頼を作る。**このコマンドはモデルを呼ばない。**

~~~powershell
python .\scripts\instruction_review_offline.py request --staging "$env:USERPROFILE\copilot-learning-staging" --fingerprint "(64桁fingerprint)" --repo "C:\path\to\repository" --policy "$env:USERPROFILE\retro-private\policy.json" --out "$env:USERPROFILE\retro-generator-request-01"
~~~

適切な利用許可と人手確認のもと、別途作られたoffline-proposals.jsonを独立Reviewer用に構造化する。

~~~powershell
python .\scripts\dual_model_review_offline.py retro-request --model-plan .\examples\dual_track_models.synthetic.json --staging "$env:USERPROFILE\copilot-learning-staging" --fingerprint "(64桁fingerprint)" --repo "C:\path\to\repository" --policy "$env:USERPROFILE\retro-private\policy.json" --proposals "$env:USERPROFILE\retro-private\offline-proposals.json" --out "$env:USERPROFILE\retro-judge-request-01"
~~~

出力のreview-request.preview.jsonは提案本文を含み得るため、**公開Gitや未承認の外部LLMへ渡してはいけない**。CLIが返したrequest_sha256を別の場所へ保存する。

独立Reviewerの回答JSONはschema_version=1.0、record_type=offline-retro-independent-review-decisions、request_sha256、およびdecisions配列を持つ。各項目はproposal_id、verdict（support_for_manual_review / revise / reject）、reasonを持つ。本人やモデルの真正性は未検証。

~~~powershell
python .\scripts\dual_model_review_offline.py retro-grade --model-plan .\examples\dual_track_models.synthetic.json --staging "$env:USERPROFILE\copilot-learning-staging" --fingerprint "(64桁fingerprint)" --repo "C:\path\to\repository" --policy "$env:USERPROFILE\retro-private\policy.json" --proposals "$env:USERPROFILE\retro-private\offline-proposals.json" --reviewer-output "$env:USERPROFILE\retro-private\reviewer.json" --expected-request-sha256 "(保存した64桁SHA)" --out "$env:USERPROFILE\retro-judge-grade-01"
~~~

結果はreview-grade.preview.json等の未適用プレビューのみ。既存AGENTS.mdやSKILL.mdは変えない。全出力はGit管理外の新規ディレクトリへ置く。

既存知識学習CLIはモデル生成に --model-id、独立Gradeに --grade-model-id を指定する。現在もrequire_online_release()で**AWS外部通信は停止**している。単一Runtimeの生成モデルと評価モデルの同一IDも拒否される。

## 今後の残件

モデルを呼ぶrunnerは未実装。実際のコスト、利用可否、精度、モデルIDとベンダーの関係は未検証。実Copilot実セッションの運用受入、意味的な矛盾判定、別Reviewerの品質のholdout検証、採用PR・ロールバックも未完成。安全停止、GitHub Actions/CI不使用、自動適用禁止を維持する。

テスト：python -m unittest discover -s tests -p test_dual_track_model_review.py -v

実装：copilot_learning/model_review_contract.py、copilot_learning/retro_llm_review.py、scripts/dual_model_review_offline.py、scripts/run_learning_poc.py、runtime_service/contracts.py。


## 独立評価を最終レビューと未適用diffへ統合する

PR #39の共通モデル設定、Phase Bの差分、提出された独立レビュー結果を重複する別スキーマに分岐させず、最後に一括して照合する仕組みを追加した。あらためてモデルを呼ぶものではない。

前提：上記retro-gradeに使用したmodel-plan JSON、検証済みsession ZIP、Phase B提案JSON、別モデルによる回答JSONを**Git管理外**で保管済み。これらは手動で提出された情報であり、実モデル利用の真正性を検証したものではない。

~~~powershell
python .\scripts\finalize_retro_review_offline.py export --model-plan .\examples\dual_track_models.synthetic.json --staging "$env:USERPROFILE\copilot-learning-staging" --fingerprint "(64桁fingerprint)" --repo "C:\path\to\repository" --policy "$env:USERPROFILE\retro-private\policy.json" --proposals "$env:USERPROFILE\retro-private\offline-proposals.json" --reviewer-output "$env:USERPROFILE\retro-private\reviewer.json" --out "$env:USERPROFILE\retro-private\final-review-01"
~~~

出力：final-review.preview.json（すべて未承認）、changes.preview.diff、manifest.json。すべての評価がsupport_for_manual_reviewの場合だけdiffの**未適用プレビュー**を保存する。一件でもrevise/rejectならdiffは空になる。支持も実際の品質・承認を意味しない。

返ってきたmanifest_sha256を別に保管し、同じ入力と出力を検証する。

~~~powershell
python .\scripts\finalize_retro_review_offline.py verify --model-plan .\examples\dual_track_models.synthetic.json --staging "$env:USERPROFILE\copilot-learning-staging" --fingerprint "(64桁fingerprint)" --repo "C:\path\to\repository" --policy "$env:USERPROFILE\retro-private\policy.json" --proposals "$env:USERPROFILE\retro-private\offline-proposals.json" --reviewer-output "$env:USERPROFILE\retro-private\reviewer.json" --out "$env:USERPROFILE\retro-private\final-review-01" --expected-manifest-sha256 "(独立保管した64桁SHA)"
~~~

元セッション/モデル計画/生成提案/独立レビューレシート/対象指示ファイル/出力のいずれかが変われば検証を拒否。実LLM実行、意味的な独立性、Copilotへの自動設定変更、外部送信、GitHub Actions/CIは追加していない。

合成テスト：

~~~powershell
python -m unittest discover -s tests -p test_retro_review_finalizer.py -v
~~~
