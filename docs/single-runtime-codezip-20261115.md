# 単一AgentCore Runtime / CodeZip統合（Issue #25）

確認日: 2026-11-15。Managed Harnessを追加しない。**CodeZipとローカル統合を実装したが、AWSへは未デプロイ。既存の外部実行停止は維持する。**

## 変更前の不足と変更後

従来の `scripts/run_learning_poc.py` と `scripts/wiki_offline.py` は別CLIであり、Runtimeの入口、S3イベント起動、ジョブ台帳、Wiki用の実モデル呼出経路がなかった。
今回追加した `runtime_service.pipeline.Pipeline.execute()` は既存の関数を直接利用し、S3の版付き入力から成果物までを実行する。既存の学習/引用/OKF検査のコード自体は変更しない。

```text
Copilot履歴をローカルで選別・承認
  → inputs/objects/ にセッションZIP・manifest・任意feedback（版付き）
  → 全オブジェクトのversionId/SHAを固定した *.job.json を inputs/ready/ に最後に置く
  → S3 ObjectCreated → Lambda
  → InvokeAgentRuntime（job単位session、受付応答まで）
  → 単一Runtimeの /invocations → DynamoDBで重複を排除 → worker
        ├─ custom: 既存抽出 → ルール評価 → 別呼出のGrader
        ├─ original_wiki: 原本 → 共通Wikiプロンプト → Bedrock → 引用検査 → OKF
        ├─ custom_and_original_wiki: 同じ原本から上の2処理（Memoryは呼ばない）
        ├─ memory_ingest: 原本・評価申告 → Memory CreateEvent
        ├─ memory_wiki: Memoryへ投入 → 取得を待つ → 暫定snapshot → Wiki → OKF
        └─ hybrid: Memory暫定snapshot → 既存候補変換 → 独立Grade
  → results/<job_id>/ に中間checkpoint、成果物、最後にcomplete.json
  → DynamoDBの状態をSUCCEEDEDへ更新
```

「単一」はRuntimeリソースが1つという意味。全実験を同じセッションに混ぜる意味ではない。Lambdaは大きな本文をRuntimeへ送らず、固定済みジョブオブジェクトの参照だけを送る。Memory/Bedrock/S3/DynamoDBはRuntimeの外部サービスである。

## 新規コード

| ファイル | 責務 |
|---|---|
| `main.py` | CodeZip入口、HTTP 8080。ローカル検証のみ `--port` を指定可能 |
| `runtime_service/server.py` | `/ping`、`/invocations`、受付202、背景worker、HealthyBusy、状態照会 |
| `runtime_service/contracts.py` | bucket/prefix/version/SHA/モデル/Memory bindingの許可リストと入力検査 |
| `runtime_service/aws_adapters.py` | 版指定S3、サービスchecksum付きPUT、DynamoDB CAS、Bedrock、Memoryページング |
| `runtime_service/pipeline.py` | 既存処理の統合、checkpoint、独立したWikiモデル、OKF出力、完了manifest |
| `lambda_function.py` / `runtime_service/trigger.py` | S3イベントの検査・Runtime起動。結果ファイルで再起動しない |
| `runtime_service/resume.py` | WAITING_MEMORYのGSI検出・条件付き予約・有限回再開（PR #27） |
| `scripts/build_runtime_zip.py` | 固定依存を同梱する再現可能ZIP。通信・デプロイなし |
| `infra/single-runtime.yaml` | 1 Runtime、1 Lambda、S3、DynamoDB、失敗通知キュー、IAMのCloudFormation定義 |

Python標準ライブラリのHTTPサーバーを使用し、AWSが許容する `/invocations` POST と `/ping` GET の契約を実装した。AgentCore SDKやManaged Harnessを必須にしない。HTTPS/IAMの入口はAWS Runtimeに委ねる。ローカルサーバーを公衆網に直接公開しない。

## 入力・設定

ジョブJSONは `schema_version=single-runtime-1`、`project`、`mode`、`session_fingerprint`、`artifacts`、`extract_model_id`、`grade_model_id`、`wiki_model_id`、`memory_profile` を持つ。使わないモデル/Memory項目はnull。`artifacts.archive/manifest/feedback` は `{bucket,key,version_id,sha256}`。feedbackは任意、archive/manifestは必須。生のCopilotファイルを無条件にS3へ送信する処理は追加しない。

モデルIDは実行環境の `ALLOWED_MODEL_IDS` にあるものだけを許可する。抽出・Grade・Wikiモデルを別々に指定可能。Memoryは入力から任意IDを指定せず、管理者設定 `MEMORY_PROFILES_JSON` 内の名前を選ぶ。各bindingには `memory_id, actor_id, namespace, strategy_ids, project` が必要。現在のcollectorでは、選択戦略のnamespaceが管理者指定の**同じproject/actor prefixの配下**にある構成を要求する。既存の設計JSONをそのままAWSへ送る機能ではない。

`CODE_SHA256` はデプロイ時に渡すZIPハッシュで、実験・checkpointのbindingに含める。ただし文字列の一致だけで実デプロイの真実性を証明するものではなく、S3のCodeVersionIdとビルド結果の照合は配備時の受入項目。

### Memoryの非同期処理

CreateEvent受領は記憶抽出の完了ではない。各イベントの安定clientTokenと受領checkpointを保存する。次に、全ページ・指定戦略・指定namespaceの結果を取得する。0件、一部戦略欠落、観測が変動中なら `WAITING_MEMORY` とし、明示retryで再開する。

連続して同じレコード集合を観測しても、全ての非同期抽出/Reflectionが完了した証明にはならない。そのため完了レポートとMemory状態レコードに `memory_extraction_complete=false`／`extraction_complete=false`、`complete_for_comparison=false` を残す。現在の既定pollは3回/5秒間隔の小規模接続確認向け。**PR #27でEventBridge Scheduler→同じLambda→同じRuntimeの有限回自動再開を追加済み**です。ただしSchedulerは既定DISABLED／EnableMemoryResume=falseで、Memory抽出完了の証拠にはなりません。詳細: [Memory再開](memory-resume-20261115.md)。

AWSの `ListMemoryExtractionJobs` / `StartMemoryExtractionJob` は失敗した抽出の再処理用であり、一般の全抽出完了確認APIとして使用していない。実比較には、専用Memory/学習範囲と完了を確認したexportの固定・検収が依然必要。

## 二重実行・再開・データ保護

- ready JSONのbucket/key/version/SHAからjob IDを決定。入力の上書きや順序逆転でも旧イベントは旧版を読む。ETagをSHAと扱わない。
- DynamoDB条件付き書込みで同じjobの並行実行を抑止。二重通知で同じ処理を新たに開始しない。
- RUNNING、SUCCEEDED、FAILED、WAITING_MEMORYを保持。受理202と処理成功を区別。期限切れRUNNINGは `RECOVERY_REQUIRED` を返し、自動的にclaimを奪わない。
- retryはFAILED/WAITING_MEMORYに対し期待revisionを必須とする。成功済みのモデル生成・Memory送信はcheckpointを再利用。
- `complete.json`は最後に書く。台帳更新前の障害では、完了manifestと成果物を照合して復旧可能。
- API成功直後・checkpoint保存前の強制終了までexactly-onceを保証しない。Memoryには安定clientTokenを使うが、Converseの応答消失では再推論・追加費用があり得る。
- S3はExpectedBucketOwner/VersionId/本文SHA/KMS ARNを検証。結果はSSE-KMS、IfNoneMatch、ChecksumSHA256で保存。同一PUT競合は実バイト列とサービスchecksumを照合してから同一扱い。
- Runtime内の一時ディスクはTemporaryDirectoryのみ。永続DBとしてSQLiteを配置しない。既存の知識台帳を自動active化しない。
- モデル入力サイズ、呼出回数、出力トークン、SDKの接続/read timeout、ジョブ時間、Memoryのページ数/poll数を制限。これはドル建ての課金上限保証ではない。料金未取得はnull。
- 例外の本文・入力・出典・パスをHTTP/ジョブエラーへ露出しない。privacyチェックは完全な企業DLPではない。

## オフラインで実行

```powershell
python -m pip install -r requirements-runtime.txt
# フルテストには既存の署名検証用cryptographyも別途必要
python -m unittest discover -s tests -v
python .\scripts\demo_single_runtime.py --out "$env:USERPROFILE\single-runtime-demo-01"
python .\scripts\build_runtime_zip.py --out "$env:USERPROFILE\copilot-runtime.zip"
python .\main.py --port 18080
```

デモはtests/runtime_support.pyの合成モデル・合成Memory・メモリ内S3/台帳を注入する。**実LLMが学習した結果ではない。** `main.py` のproduction経路は同じテスト用注入へ切り替えられず、既存 `require_online_release()` を通る。環境変数で停止を解除する機能はない。`GET /ping` は200、未承認の `POST /invocations` は503/ONLINE_RELEASE_BLOCKEDになる。

ビルダーはインストール済みの固定バージョンを同梱し、純Pythonコードだけを収録する。PyYAMLの任意C拡張を除き、純Python SafeLoaderで動くことを検証する。x86_64バイナリをARM64向けとして配らない。ライセンス/依存metadataとBUILD-MANIFESTを同梱し、データ・テスト・鍵・辞書はZIPから除外する。今の経路はcryptographyを呼ばず、社内署名検証の実統合は安全停止解除前の別工程。ネイティブ依存を追加する場合はARM64 wheelの収録方式またはDockerを別途整備する。

## 配備定義（この変更では実行しない）

同じZIPをRuntimeのCodeConfigurationとLambdaのコードに使用する。RuntimeはPYTHON_3_13 / main.py、Lambdaはpython3.13 / arm64 / lambda_function.lambda_handler。CloudFormationは新しいprivate/versioned/KMS bucketとジョブDynamoDB、S3通知、Lambda、1 Runtimeを定義する。データbucket/台帳はRetain、通知は初期false。

配備前に必要な値: code bucket/key/version、ZIP SHA、会社承認KMS、許可モデルID/ARN、Memory binding/ARN、必要な辞書。コードZIPのKMSはこのテンプレートではDataKmsKeyArnと同じキーを使う前提。AWS制約・KMS key policy・Runtime実行ロール・リージョン・モデル利用権限・Memory戦略を実環境で検証する。PUBLIC networkはAWS Runtimeのネットワーク指定であり、S3公開の意味ではない。社内VPC/endpoint必須条件は別途反映が必要。

**通知をtrueにしてもコードの送信停止は解除されない。CloudFormationの用意とデプロイ成功・社内承認を混同しない。** 現段階の実運用投入は禁止。LambdaのDLQは起動失敗用で、受付後にRuntimeで失敗したjobはDynamoDB/結果参照による監視が別途必要。

## 今回確認したこと / 未確認

- 既存の関連131テストを変更前にPASS確認。改修後は新規64件＋関連既存131件＝195件PASS（全リポジトリの全テストではない）。
- 新規64テスト: 六つのモード、Lambda→Runtime→OKF、actual localhost HTTP、重複/改変/途中再開、SDK引数形、checksum、ページング、CodeZip自己完結import、production外部通信拒否。
- 依存同梱ZIPをLinux x86_64/Python3.13.5の `-I -S`（site-packagesを使わない）で実起動。/ping 200、/invocations 503を確認。8080が使用中のためローカルsmokeだけ別port。AWSでの既定は8080。
- 六モードの合成E2Eを実行。実AWS/Memory/LLM呼出しとGitHub Actions実行は0。
- **PR #27でMemory待ち自動再開コードを追加済み**（関連98件の合成テストPASS）。AWS ARM64/AL2023起動、CloudFormation実デプロイ、会社認証/DLP、実Memory抽出完了・実性能比較、39条件全体のrunner、実Copilotは未検証または未実装。Issue #25と関連IssueはOPENのまま。

## 一次仕様（2026-11-15確認）

- CodeZip/依存/entrypoint: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-code-deploy-python.html
- 非同期/HealthyBusy: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-long-run.html
- Runtime CFN: https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-bedrockagentcore-runtime.html
- CodeConfiguration: https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-bedrockagentcore-runtime-codeconfiguration.html
- InvokeAgentRuntime: https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_InvokeAgentRuntime.html
- S3イベント・versionId・URL encoding: https://docs.aws.amazon.com/AmazonS3/latest/userguide/notification-content-structure.html
- Memory一覧とpagination: https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_ListMemoryRecords.html
- 失敗抽出の再処理API: https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_StartMemoryExtractionJob.html
