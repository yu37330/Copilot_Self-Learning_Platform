# Issue #31 Phase C：指示改善の変更前後をペア比較する（オフライン評価）

更新：2026-11-19。Phase Aのセッション振り返り、Phase Bの未適用diffの続きとして、**同じ未学習タスクに対する変更前/変更後の提出済み観測結果**を評価する仕組みを追加した。GitHub Copilot、LLM、AWSを実行する機能ではない。実際の指示ファイルを書き換えない。

## 初期デモ（AWS・Copilot不要）

Git管理外の新規ディレクトリを出力先に指定する。

~~~powershell
python .\scripts\demo_instruction_effect.py --out "$env:USERPROFILE\copilot-retro-phase-c-demo-01"
~~~

デモは手作りの合成5タスク・2条件で作成されるため、実際の品質改善を示すものではない。出力はdataset.synthetic.json、observations.synthetic.json、observations.blank.json、comparison.synthetic.json、manifest.jsonで、どのファイルもGitへコミットしないこと。

## 実際に比較評価するための入力

### dataset.json

必要な情報は次の通り。

- schema_version=1.0、record_type=offline-instruction-effect-holdout
- label_source=synthetic または human_verified（**申告値であり認証ではない**）
- training_group_ids：学習に使った障害/開発系列IDのリスト。少なくとも1件
- training_task_sha256：学習タスクの正規化fingerprintのリスト。空でもよいが、漏えい検査の証拠は弱い
- proposal_review_sha256：Phase Bのレビュー/manifestのSHA（値の真正性は人手確認）
- evaluation_protocol_sha256：事前に決めた正解判定ルールのSHA
- instruction_snapshots.baseline / candidate：変更前後の指示セットの異なるSHA。実際にCopilotへ適用した事実は確認できない
- cases：case_id、group_id、task、task_sha256、familyを持つケース（1〜500件）

同一タスクの文字列fingerprint重複、train/testのgroup_id重複、case_id重複、対象の指示版一致を拒否。類似文・意味的重複や、別系列への知識流出を完全に検出するものではない。評価ケースの本文と正解判定基準を事前に固定し、改善案作成に利用しないこと。

### observations.json

schema_version=1.0、record_type=offline-instruction-effect-observations、dataset_sha256、armsを持つ。armsはbaseline/candidateの2種類、どちらも未実行ならnull。実行済みの各armには、source_kind、instruction_sha256、profile、executor_sha256、casesが必要。

profileはmodel_id、budget_tokens、tool_policy、sampling_seedを含む。両armのprofileが異なる場合は拒否。source_kindはsyntheticまたはimported_replay。モデルのバージョン、トークン上限、ツール権限、seedは揃えるが、実行環境/順序/時間差まで統制した証拠にはならない。

各caseの観測には次の項目を入力する。

- run_id、trace_sha256（一意）
- outcome：success / failure / inconclusive
- reviewer_sha256、review_receipt_sha256：success/failureには実行主体と異なるレビュー主体のハッシュと検収証跡SHAが必須。本人/組織の身元認証はしない
- tool_calls、retry_count、input_tokens、output_tokens、latency_ms、cost_usd：非負の実測値またはnull。未観測値を0で補完しない

同じケースに対して変更前/変更後の観測が欠けると比較をしない。ケース単位の保留はinconclusiveとして残し、成功率の分母には混ぜない。未知の費用・トークンはnullを維持する。

## ローカルCLI

まず入力データのSHAと形式を確認する空テンプレートを作成する。

~~~powershell
python .\scripts\evaluate_instruction_effect_offline.py blank --dataset "$env:USERPROFILE\retro-private\dataset.json" --policy "$env:USERPROFILE\retro-private\policy.json" --out "$env:USERPROFILE\retro-private\observations.blank.json"
~~~

実行済み観測をローカルで入力し、集計だけを行う。

~~~powershell
python .\scripts\evaluate_instruction_effect_offline.py evaluate --dataset "$env:USERPROFILE\retro-private\dataset.json" --observations "$env:USERPROFILE\retro-private\observations.json" --policy "$env:USERPROFILE\retro-private\policy.json" --out "$env:USERPROFILE\retro-private\result-01.json"
~~~

実データ（label_sourceがsynthetic以外）のデータセットはGit管理外に保存し、--policyの指定が必須。出力の上書き・Git管理下への出力は禁止。コマンド実行でCopilotのセッション本文を収集したり、外部サービスへ送信したりしない。

## 結果に含まれる集計

| 指標 | 定義 |
|---|---|
| success_rate | 各armのsuccess / (success + failure)。inconclusiveは別に集計 |
| wilson_95 | 各armの成功率に対するWilson 95%区間（統計的な効果証明ではない） |
| delta_success_rate | 両armで結論のある同一ケースに対する、candidate-only成功 − baseline-only成功の比率 |
| baseline_only_success | 指示変更後に成功から失敗へ変わった比較ケース数 |
| candidate_only_success | 指示変更後に失敗から成功へ変わった比較ケース数 |
| mcnemar_exact_two_sided_p | 不一致ペアの両側二項検定。0件ならnull。無作為化や独立性が保証されないため参考値 |
| delta_metrics_candidate_minus_baseline | 同一ケースで両側が計測された値の差。未知値を補完せずn_paired_observedを返す |

最低30の有効ペアに満たない場合はsmall_sample_warning=true。統制不足や実セッション証跡の未検証はケース数と無関係に残す。real_copilot_execution_verified、instruction_snapshot_installation_verified、causal_improvement_establishedは**常にfalse**で、統計値をもって自動的にtrueにはしない。

比較に使ったPython処理はケース本文を出力結果へ転載せず、指示セット・学習/評価プロトコル・提出観測のSHAを出力する。ただし実データが入った入力ファイルそのものは機密扱いで保管し、DLPと承認を別途確認する。

## 現行の未完了事項

- 実Copilot CLI・デスクトップのイベント形式への対応。対象Windowsの既定~/.copilot/session-stateは現時点では未検出
- 実LLMを利用した改善理由の文脈的・意味的分析、独立したセキュリティレビュー
- 固定した指示セットをCopilotへ適用し、同一課題で計測値を採取する実験runner
- レビュー者の真正性、評価結果の原本と同一性を確認する署名・認証
- タスク順序、評価時間差、LLMの非決定性、漏えい・交絡要因を考慮した実験設計
- 既存Windowsテストで残っているSQLite/symlinkエラー（Issue #34）

GitHub Actions/CI、AWS送信、自動変更機能は追加しない。既存require_online_release()によるオンライン実行の安全停止を維持する。

## 合成回帰テスト

~~~powershell
python -m unittest discover -s tests -p "test_instruction_effect_eval.py" -v
~~~

上記の合成テストPASSは実Copilotの成績向上を意味しない。
