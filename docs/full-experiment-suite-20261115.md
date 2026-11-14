# 全比較条件を先に準備する検証セット

2026-11-15 / Issue #10・#2。**「標準の検証後に他方式を準備」ではなく、比較対象・共通入力・評価条件・必要な実行経路を最初に揃える。実行順序と準備範囲を分離する。** 旧Memory設定案の段階導入は「実行の順番」であり、準備を遅らせる方針ではない。

## 現在の到達点

13構成の設計プロファイル、共通条件、ケース別のジョブ計画、未実行の観測枠を一括生成できる。これは**実験の準備計画であり、AWS APIリクエストでも、全実行器が完成したという宣言でもない**。AWS/モデル/Copilotへの呼出し、リソース作成、既存台帳の変更はない。既存の外部I/O停止は維持する。

## 最初から準備する13条件

| ID | 内容 | 切り分けたい効果 |
|---|---|---|
| D | 知識なし | 対照群 |
| A0 | 標準Episodic | 経験・Reflectionの基準 |
| A1 | 用途向けEpisodic override | 標準とカスタム設定の差 |
| A2 | 標準Episodic＋Semantic | 安定した事実を加える効果 |
| A3 | 標準Episodic＋Summary | セッション再開支援を加える効果 |
| A4 | 標準Episodic＋User preference | 個人の希望を加える効果 |
| A5 | 4戦略すべて標準で有効 | 全部入りの利点・ノイズ・費用 |
| S | Semantic単独 | 事実記憶だけの比較 |
| Q | Summary単独 | 要約だけの比較 |
| U | User preference単独 | 個人設定だけの比較 |
| B | AgentRun型の独自抽出・検査 | 自作エンジンとの差。AgentRun本体ではない |
| C0 | A0の同じ抽出結果＋独立Grade | 検査追加の効果 |
| C1 | A1の同じ抽出結果＋独立Grade | カスタムMemoryへの検査追加の効果 |

全組合せ・全数値の総当たりではない。主な機能を単独／追加／全部入りで比較する初期の実験行列。結果を見る前に条件を確定し、追加実験は別のsuite IDとして記録する。

## 共通に固定する条件

- 同じ訓練セッション・フィードバックの確定スナップショットSHA、同じ未学習ケースを使う。準備処理はSHAの形式を検査するだけで原本存在は証明しない。実行器で原本から再計算する。
- downstreamの解答モデル・トークン上限・ツール権限・検索後の文脈予算を共通化。例の検索上限6件、文脈2000トークン、総予算8000トークンは**提案値**。
- 複数戦略では各戦略に2000トークンを与えず、**統合後の文脈全体**に同じ上限を適用する。切り詰め・選択方法も実行器で固定する。
- Memoryはプロジェクト×構成×反復で別リソースに分離する計画。C0/C1は新しい抽出をせず、対応するA0/A1の同一抽出成果物SHAを使う。isolation keyは割当計画でありIAM認可ではない。
- 学習後に知識版を確定し、評価期間は学習しない。後の評価ケースやその正解をMemoryへ取り込まない。結果回収と次回の学習は、評価セットの固定を解除した別サイクルで扱う。
- 学習/評価のグループとタスクハッシュを比較。言い換えや同じ障害の系列重複は人手でも確認する。
- 実行ジョブにはタスクのハッシュを出し、本文や正解を入れない。実行器へタスク本文を渡すアダプターと、正解を使う採点器は分離する。
- 作業完了率・誤適用率・保留率・再発率・ツール回数・所要時間・費用を測る。今の適用/非適用ラベル評価を、開発タスクの成功率と混同しない。

## 費用とモデル比較の注意

抽出・統合・Reflection・Grade・検索・回答・ツールの費用を分ける。Cの増分費用はGrade等である一方、独立運用の総費用には元の抽出コストも含める。実験内の再利用による費用削減と、製品運用時の総費用を分ける。

AWS標準Memoryの内部モデルと、自分で指定するoverrideモデルが同じとは仮定しない。A0対A1は設定全体の比較。プロンプトだけの影響を測るときは、同じ指定モデルで「標準指示を保持したoverride」と「改訂指示のoverride」を別途追加して比較する。

## Memory設定テンプレートの方針

各設計プロファイルに、戦略、namespace、短期30日／長期90日の保持**提案**、promptプロファイルを含める。実リージョン・KMS・Memory ID・権限・SDK対応は部署環境で検証する。これらはCreateMemoryにそのまま渡すJSONではない。

A1には用途向けの編集方針を同梱するが、完全なoverrideプロンプトではない。標準指示からの編集、出力スキーマ維持、選択モデル、実API形式の確認は未完了。標準とカスタムを同じMemory内で逐次切替えて以前の知識を混ぜない。

AWS公式で確認した役割と制約（2026-11-15）:

- [組み込み戦略](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/long-term-configuring-built-in-strategies.html): Semantic、Summary、User preference、Episodicを目的別に構成する。
- [Episodic](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/episodic-memory-strategy.html): 抽出・統合・Reflection、TOOL結果、Episode完了後の非同期生成。完了待機と取得0件の理由判別が必要。
- [カスタマイズ](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-custom-strategy.html): `appendToPrompt`は標準指示の置換。出力スキーマ・統合操作名を変更しない。実行ロール・モデル権限・費用の追加条件がある。

## 準備CLI

Git管理外の新規フォルダへ出力する。`training-snapshot-sha256`は実際には確定入力から計算したSHAを指定する。例のSHAとモデルは配線確認用の合成値。

```powershell
python .\scripts\prepare_experiment_suite.py `
  --dataset .\examples\full_suite_dataset_synthetic.json `
  --project synthetic `
  --training-snapshot-sha256 ('a' * 64) `
  --model-id synthetic-model `
  --repetitions 2 `
  --out "$env:USERPROFILE\copilot-full-suite-01"
```

出力: `experiment.json`、`memory_profiles.design.json`、`jobs.json`、`observations.blank.json`、`manifest.json`。
6ケース×13構成×2反復なら156ジョブの**計画**ができる。観測値は全てnull。ジョブを実行したり、モデルに正答させたりした結果ではない。

`manifest_sha256`を別途保管する。これは完全性照合用で、社内署名承認の代用ではない。

## 評価課題も先に揃える

障害解決だけでSummary/User preferenceを採点すると不公平になる。troubleshooting、project_facts、session_resumption、preferences、negative_scopeの5領域を揃え、領域別の結果も出す。準備処理は不足領域を明示する。

同梱の6件は**準備経路の配線確認用**であって本番ベンチマークではない。信頼できる性能比較には、過去の完了した開発タスクから独立した正解、テスト、適用／不適用条件を別途用意する。

## 「準備完了」の定義と残作業

| 項目 | この変更 |
|---|---|
| 全13比較構成、共通条件、分離方針 | 定義・生成済み |
| 同一入力から全ジョブを準備、結果はnull | 実装・合成テスト済み |
| AWSリソース作成リクエストと完全なoverride prompt | 未完成 |
| Memory作成→投入→抽出完了待機→取得→確定 | 実行アダプター未完成 |
| B/C/Dの実タスク実行・証跡回収 | 共通実行器未完成 |
| Cの抽出結果の同一性の強制検証 | 計画で要求。実行器での実装は残る |
| 全構成の観測値を既存A/B/C/D評価器へ接続 | 変換アダプターは未完成。13キーを直接渡さない |
| 失敗時の再開・タイムアウト・冪等性・費用上限 | 共通実行器での実装・検証が残る |
| Windows/AWS/Copilot、社内認証・DLP | 実機未検証 |

準備項目はすべて最初からIssue #10の範囲に置き、A0の成績待ちを他方式の実装開始条件にしない。実リソースの作成・課金・外部送信は従来どおり別ゲート。今回の変更だけで全実験が実行可能になったとは表現しない。
