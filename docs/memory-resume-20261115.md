# 単一RuntimeのMemory待ちを有限回再開する

**更新: 2026-11-15 / [PR #27](https://github.com/yu37330/Copilot_Self-Learning_Platform/pull/27)をmainへマージ済み。** PR #26を基準にしたコード・合成テストです。AWSへのデプロイ・実送信・Scheduler有効化はしていません。対象: Issue #25 / #2 / #6。[最新状況](current-status-20261115.md)。

## 問題と変更

以前はMemoryから0件または変動中の結果を受け取ると `WAITING_MEMORY` へ遷移し、毎回手動の `retry` が必要だった。今回、同じ起動Lambdaに定期点検を追加し、待機中のジョブだけを同じ単一Runtimeへ再投入する。Managed Harness・二つ目のRuntime・二つ目のLambdaは増やさない。

```text
S3 ready manifest → 起動Lambda → 単一Runtime
                                  ├─ 結果あり → 既存検査/OKF → S3
                                  └─ 未取得/変動中 → DynamoDB WAITING_MEMORY
EventBridge Scheduler（既定DISABLED）
  → 同じ起動Lambda → 期限が来た待機ジョブを条件付き予約 → 同じRuntimeのretry
```

これはMemoryが抽出をすべて終えたことを判定する機能ではない。既存の `memory_extraction_complete=false` / `complete_for_comparison=false` を維持する。生成済みの出力は引き続き未承認・暫定であり、実験結果の品質認定を意味しない。

## 再開条件

- 対象は `WAITING_MEMORY` かつ次回時刻を過ぎたジョブだけ。FAILED、SUCCEEDED、RUNNING、RECOVERY_REQUIREDは対象外。
- 初回実行時にreadyファイルのbucket/key/VersionId/SHA、job ID、実行設定のハッシュを台帳へ保存する。タスク本文・モデル出力・機密辞書の本文は追加で台帳へ写さない。
- 設定ハッシュはコードSHA、モデル許可リスト、Memory範囲、機密辞書、処理上限、再開ポリシーを含む。変更があれば自動再開を止める。
- GSIは待機ジョブの候補を検索するだけ。台帳本体を `ConsistentRead=True` で再取得し、状態・revision・due時刻が一致する場合だけ予約する。
- 予約だけではRuntimeの処理成功としない。Runtime側の既存revision/token付きclaimが実行開始を確定する。

### 初期値（実測から決めた最適値ではない）

| 項目 | 値 |
|---|---|
| 定期点検 | 1分間隔、配備定義の初期状態はDISABLED |
| 再確認間隔 | 60 → 120 → 240 → 480 → 900秒、以降900秒 |
| 自動再確認の待機回数 | 初回処理後、最大8回分を予約 |
| 総待ち時間 | 初回実行から最大7200秒 |
| 起動送信の回数 | 応答消失等も含め最大16回 |
| 起動結果不明時の保留 | 120秒 |
| 一度に検索する件数 | 最大5件、古い期限から |
| Lambda時間残量 | 65秒未満なら次のジョブを始めない |

実行時刻はSchedulerの実行・キュー・GSI反映の遅延を含むため、due時刻は最短の再開予定であり正確な秒数を保証しない。上限は処理回数・時間の制御であって、ドル課金上限ではない。

## 重複・失敗時の扱い

同じ定期イベントが二重配送されても、台帳の条件付き予約で抑止する。予約はrevisionを変えず、Runtimeへ渡す `expected_revision` を維持する。同じjob/revisionの送信は同じRuntime session IDを使用する。

起動後に応答が消失した場合、状態をWAITINGへ巻き戻さない。120秒後、台帳本体を読み直す。すでにRUNNING/SUCCEEDEDなら追加起動しない。実行されていない場合だけ同じrevisionで再送する。既存のS3 checkpointを再利用し、保存済みのMemory CreateEventやモデル結果を無駄に再送しない。

処理クラッシュで期限切れRUNNINGになった場合は、従来どおり `RECOVERY_REQUIRED`。自動的に奪取しない。API成功直後かつcheckpoint保存直前のクラッシュまでexactly-onceを保証するものではない。

回数・総時間の超過や設定差替えは `RECOVERY_REQUIRED` に移し、理由を `MEMORY_WAIT_LIMIT`、`MEMORY_WAIT_DEADLINE`、`RESUME_DISPATCH_LIMIT`、`RESUME_BINDING_CHANGED` 等で記録する。未抽出を「失敗した知識」や「比較成績0」とは扱わない。

一回の定期点検で一部の起動に失敗しても、他ジョブは確認可能。戻り値とCloudWatchへ件数だけを出力する。本文・参照パス・Memory IDは記録しない。継続エラーの通知先・運用監視の外部連携は別途設定が必要。

## 配備上の変更

`infra/single-runtime.yaml` は既存DynamoDBへ `memory-resume-due-v1` GSIを追加し、EventBridge SchedulerのGroup/Role/Scheduleを追加する。Schedulerの権限は同じLambdaのInvokeと既存DLQへのSendMessageに限定する。Lambdaには台帳のGetItem/UpdateItemとこのGSIのQueryを追加する。Scan権限、Bedrockモデル直接呼出し権限、Memoryへの直接投入権限は追加しない。

`EnableMemoryResume=false` が既定。S3通知の既定OFFも維持する。フラグをtrueにしても `require_online_release()` は解除されない。実環境承認なしで機微な入力を送信しない。

旧版が残したWAITING_MEMORYには原本・設定を結び付けた予約情報がないため、自動で拾わない。既存ジョブの再実行は新しいコードと入力のレビューが必要。GSIはバックフィル/ACTIVE完了と権限を実環境で確認してからSchedulerを有効にする。コードや機密辞書等の更新で既存待機ジョブが停止することは意図した安全境界。

## 検証

```powershell
$env:PYTHONPATH = "tests"
python -m unittest test_memory_resume test_single_runtime test_runtime_aws_and_package -v
python scripts\build_runtime_zip.py --out "$env:USERPROFILE\copilot-runtime-resume.zip"
```

合成アダプターで「0件 → WAITING_MEMORY → 定期点検 → retry → Wiki生成/OKF」を通す。起動前/起動後の応答消失、重複点検、期限/回数、設定改変、残時間、GSIの古い行、ネットワーク停止を検査。BotocoreによるAPI引数の形の検査と、CloudFormation静的検査を行う。

DynamoDBの実際の条件式評価やGSI反映遅延、Scheduler配送、IAM、ARM64/AL2023上の起動はモックでは証明できず未検証。39条件全体の実験ランナー、Memory全戦略の作成/抽出完了検収、社内認証/DLPは別の残課題。

## 一次資料（2026-11-15確認）

- EventBridge Scheduler / CloudFormation: https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-scheduler-schedule.html
- Scheduler Target/Role/Retry: https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-scheduler-schedule-target.html
- DynamoDB GSIは結果整合: https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/GSI.html
- テーブル本体の強整合読取: https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/HowItWorks.ReadConsistency.html
- Runtimeの非同期処理とHealthyBusy: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-long-run.html
