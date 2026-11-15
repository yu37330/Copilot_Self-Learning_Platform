> **対象に関する補足（2026-11-15）**：このプロジェクトはWindows PCでGitHub Copilotを使う個人・チーム・企業の開発者を想定します。以下の『会社』『社内』は当時検討していた管理環境の履歴上の表現で、企業専用を意味しません。AWSの外部通信停止は利用形態にかかわらず維持します。

# 外部レビューへの対応 第1弾（v0.3・PR #4でmain反映済み）

## 対象・結論

対象mainコミット: `31a87f2960b58c74a85503d2629ff741983ec4f4`。
対象tree: `6a79d73aca1ab2563ab6b86a8fb8c99618ccf979`。
修正前のローカルソース19ファイルから生成したGit treeが上記と一致することを確認した。

これは当初ローカル修正案として検証した記録で、その後 [PR #4](https://github.com/yu37330/Copilot_Self-Learning_Platform/pull/4) でmainへ反映した。以下の検証記録は当時のLinux/Python環境の実測であり、Windows/AWS実機を検証した意味ではない。

既存32件のテストが通る一方、レビューにある「ビルド失敗を引用した成功Skill + 無関係な文書確認成功 → keep」を修正前に再現した。テストの成功は網羅性や学習品質を保証しない。AgentRun同等以上の学習品質は引き続き判定できない。

## この修正案の重要な動作変更

**公開CLIからのAWS送受信を一時停止する。** `-Upload`、`--execute`、`--use-bedrock`は外部実行しない。認証確認のSTSも呼ばない。ローカル梱包・正規化・候補評価・Dry Runは継続できる。

理由: ローカルの自己申告承認を社内IDに結び付けた正式承認として扱えないこと、フィードバックの承認連結、DLP、全送信経路の統合検証が未完成であること。環境変数や確認フラグで停止を解除する仕組みは設けていない。再開には別の実装修正とレビューが必要。

これは既存本番環境を停止したという意味ではない。パッチの適用後だけ有効になる安全対策であり、既存mainは変更していない。低レベルの注入可能クライアントは合成テスト向けで、悪意あるコード改変者を隔離するサンドボックスではない。

## 指摘別の対応状況

| 指摘 | 第1弾での変更 | 残る事項 |
|---|---|---|
| セッション全体の成功で誤採用 | `candidate_sha256`、引用ID、対象セッション、候補への明示評価を照合。関連失敗・反証・イベント欠落はpending。独立Graderに証拠本文、適用範囲、フィードバックを渡す | 人による候補評価も誤り得る。未学習タスクの性能評価は別に必要 |
| Memory計画の未承認本文 | 検証したZIPスナップショットから再生成した計画と全項目比較。送信対象は再生成オブジェクト。本文・actor・時刻・token・追加イベントを拒否 | 完全な署名承認と実送信経路の検証は未完成。実送信は停止 |
| Memory exportの範囲 | operation/Memory/actor/namespace/region/account/入力ハッシュの契約比較を追加 | namespaceが実際にどの記憶を含むかは戦略設定で検証が必要。広いnamespaceを自動的に安全とは判断しない |
| 自己申告承認・後付けfeedback | 操作別の契約ハッシュを比較する下地を追加。既存review_scopeを正式な社内認証と誤解しない説明へ変更 | 社内ID、署名者の権限、改ざん耐性、期限・失効、feedback・モデルの承認接続は未完了。外部停止で暫定封じ込め |
| S3 TOCTOU/自己申告SHA | 不変bytesスナップショット、SHA-256のBase64チェックサム、expected-bucket-owner、サービス側チェックサム・KMS照合のプロトコルを新規実装。競合も一致時だけ成功 | **新ヘルパーは既存PowerShellのオンライン送信経路にはまだ接続していない**。元経路は停止。S3実サービスは未検証 |
| PowerShell 5.1の文字コード | `upload_sessions.ps1`をUTF-8 BOM付きへ変更。5.1/7向け構文解析・日本語空白パスのDry Run検証スクリプトを追加 | 現環境にはPowerShellがなく実行未検証。BOMの存在のみ機械検証済み |
| 自由記述/プロンプト誘導 | 命令をConverseのsystemへ分離。Graderの理由も既知秘密パターンで検査。該当理由を保存せずマスクしpendingへ | メール・顧客名等の包括的PII/DLPや組織辞書、仮名化、偽陽性測定は未実装。system分離だけで誘導を防げるとは主張しない |
| 再梱包/識別子 | 同じ梱包は既存manifestを検証・再利用。読込時にもfingerprintを再計算。時刻補完の利用回数を明示 | セッション終了の厳密判定、時刻逆転・部分作成の並行実行は追加検証が必要 |
| 部分失敗/出力先 | Memoryの出力先が既存の場合は外部処理前に拒否 | 受領情報のイベント別ジャーナル、応答消失、再開の完成は未実装 |
| イベントの脱落 | 理由別・位置別スキップ記録、補完時刻の明示、欠落のある入力の採用保留。actorのコロン対応、実送信計画のMemory ID形式検証 | 実Copilotスキーマと重要イベント認識は未確認。現在の保留は安全側だが棄却・保留率を高める |
| A/B/C比較の非対称性 | レポートに`methods_comparable=false`、ZIP/manifestのダイジェスト、未取得のS3版をnullで記録 | 共通評価器、全Memoryレコードの根拠対応、既存知識台帳、分割・適用/非適用課題、実費測定は未実装 |
| Converseの打ち切り・usage | stopReasonを検査し、打ち切り・非テキスト応答を拒否。usage/modelIdをプロバイダーに保持 | 永続的な全呼出し台帳・コスト換算、実モデル互換性は未完成 |

## 代表的なコード差分

修正前はセッション単位で `verified = any(...verified_success...)` を計算していた。

修正後は `candidate_feedback(item, session, feedback)` により、候補本文・一般化の主張・引用のダイジェストが一致した明示評価だけを確認する。候補の文言が変われば評価の流用を拒否する。失敗の引用や反証があればpendingとし、関連のない成功では昇格させない。legacyのセッション単位フィードバックは保存できるが採用根拠にはしない。

独立Graderは `independent_llm_grade(provider, graded, session, feedback)` に変更し、証拠本文・反証・一般化を渡す。コンテキストがなければモデルを呼ばずpendingにする。LLMの判断でルール上のdropをkeepへ戻すことはしない。

## 検証結果

環境: Linux / Python 3.13.5。実AWS認証、実Copilotログ、会社データは使用していない。

- 修正前: 既存32テストPASS。外部レビューの誤採用も再現。
- 修正後: 59テストPASS（既存32件を新しい契約に合わせて更新、回帰27件を追加）。削除したテストはない。
- 正例テストの入力は、イベント欠落のない入力と候補単位の明示評価へ更新した。旧式フィードバックがkeepにならないことは別回帰テストで確認。
- Memoryの本文・actor・時刻・token・イベント追加を改変すると、再生成の比較段階で拒否。送信モック0回を検査。
- S3の元ファイルを検証後に変更しても、ヘルパーが扱う送信内容は検証済みbytes。メタデータだけ一致する既存物は拒否。
- 合成デモ完走。A/Cはnot_executed、Bは候補1件pending。改善実証なし。
- Python compileallとgit diff --checkを通過。
- Windows PowerShell 5.1/7、AWS実サービスでの検証は未実施。

## 再現・確認

```powershell
python -m unittest discover -s tests -v
python .\scripts\demo_offline.py --out "$env:USERPROFILE\copilot-review-demo-unique"
```

Windows検証可能時は、それぞれのエンジンで以下を実行する。AWS資格情報は不要。

```powershell
powershell.exe -NoProfile -File .\tests\Test-PowerShellOffline.ps1
pwsh.exe -NoProfile -File .\tests\Test-PowerShellOffline.ps1
```

次のレビューでは、候補への評価の結び付けを再検証し、署名承認と送信用最小データのスキーマを確定したうえで全送信経路を統合する。**停止ガードを単に削除して再開しないこと。**

## 仕様確認に用いた一次資料

- AWS CLI PutObject: https://docs.aws.amazon.com/cli/latest/reference/s3api/put-object.html
- AgentCore CreateEvent: https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_CreateEvent.html
- Bedrock Converse: https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html
