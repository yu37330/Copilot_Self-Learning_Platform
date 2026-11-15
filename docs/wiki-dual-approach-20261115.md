# Wiki二方式の同時準備（Issue #21）

**更新: 2026-11-15 / PR #27まで。** 以下のオフラインCLI説明は当初の仕様です。PR #24でOKF v0.2、PR #26で単一RuntimeからのBedrockモデル呼出経路、PR #27でMemory待ち自動再確認コードを追加しました。**実AWS/実LLM/実Memoryでの生成品質は未検証**。外部送信停止は維持します。[現状](current-status-20261115.md)。

## 1. 変更前の問題と変更内容

以前は個々のNote/Skillの台帳・検索・利用結果はあったが、複数出典をまとめるWikiページ、ページ間リンク、矛盾候補・古い出典の点検がなかった。今回、新規モジュールとCLIで次を追加した。既存モジュールは変更しない。

| 方式 | 知識作成の経路 | 今回実装した入口 |
|---|---|---|
| W0: Wikiなし | 既存Memory/Note/Skillの経路 | 比較の対照条件 |
| WM: AgentCore型 | 原本→AgentCore Memory Semantic/Episodic→確定レコード→共通Wiki編集器 | ローカルのListMemoryRecords結果の取り込み、出典/namespace/strategy検査 |
| WO: 独自原本型 | 同じ原本→独自LLMで整理・統合→共通Wiki編集器 | SHA付き文書の取り込み、共通編集プロンプト、候補の取り込み |

WMはAWS標準の「Wiki機能」ではない。Memoryが抽出/統合/Reflectionを担当し、ページ編集・Markdown・リンク・版管理はアプリ側。**単一Runtimeのモデル呼出経路は実装済みだが、AWSデプロイと実推論は未実施**。WOも同じBedrock推論モデルを指定できるため、独自方式＝AWS禁止ではない。

## 2. 今回動く処理と、動かしていない処理

両方式とも、オフラインCLIでは入力固定→プロンプト準備→提出されたWiki候補の引用検査→ページ/リンク出力→再検証→日本語検索→Lintが動く。**単一Runtime経路ではBedrock ConverseのWiki候補生成呼出しも実装済み**です。ただし実モデルは呼んでおらず、デモ候補は合成・手作り。自律的な反復編集・Memoryの実抽出・Wikiによる性能向上は未実証です。

`wiki_prompt()` は同じ完全な出力契約を両方式に渡す。`compile_wiki()` は各主張の `key/value/statement/applies_when/evidence` を検査する。source_idと逐語引用が入力に実在することを要求するが、主張を支持するかどうかの意味判定・事実認定はしない。

Memoryの引用は `derived_memory` とし、record ID、strategy、namespace、レコードSHAを保持する。存在しない元イベントIDを補わない。独自原本は `raw_document` とするが、そこに書かれた事実も真実と自動認定しない。両方式とも `manual_review_required`、`approved_for_copilot=false` のまま。

## 3. Wikiの状態・更新・検索

ページには検査済みの内部リンクと主張ごとの引用を持たせる。新しい版は別の新規フォルダへ保存し、前版SHAを参照する。原本も旧版も変更しない。これは版の参照管理であり、署名済み承認・並行更新の中央台帳・Gitへの自動保存ではない。

Lintは**同じclaim key・同じ適用条件でvalueが異なる候補**を指摘し、どちらかを自動採用しない。一般的な意味矛盾、言い換え・時系列による解消は未対応。現原本が変わった、消えた、または入力snapshotが更新された場合、旧版から検索結果を返さず再構築を要求する。

検索は英数字+日本語2-gramの重なりによるレビュー用ページID検索。BM25・意味検索の実性能比較ではない。衝突候補を含むページを除外し、自動回答やSkills反映はしない。

## 4. 入力契約と安全性

- 原本: `{"documents":[{"source_id":"s1","title":"...","text":"...","sha256":"UTF-8本文SHA"}]}`。
- WM: `memory_id,namespace,strategy_ids,response` のオブジェクト。responseは取得済み `memoryRecordSummaries` と任意nextToken。
- `nextToken`が残る不完全取得、範囲外namespace、異なるstrategy、同一IDの重複は拒否。次ページを勝手に無視しない。
- `expected_input_sha256`は**json_bytesで正規化した入力JSON**のSHA。元ファイルの任意の空白付きバイト列のSHAとは異なる。本文SHAはUTF-8原文そのもの。
- training SHAは比較用の申告値であり、元のS3原本との同一性はまだ証明しない。入力本文/引用のSHAは実際に再計算する。
- 秘密情報パターン、PII、組織固有語、危険指示、制御文字を検査。完全なDLPではない。
- Git内、自動読込ディレクトリ、リンク/ジャンクション経由、既存出力への上書きを拒否。Markdown本文のHTML/リンクはエスケープする。
- `manifest.json`のSHAを別経路で保管してverifyする。同権限者が原本・manifest・外部SHAを全部変える攻撃への正式監査対策ではない。ACL/暗号化/SSOは別途。

## 5. 実行例

```powershell
# AWS/Copilot不要。新しいGit外フォルダに2方式の合成Wikiを生成する
python .\scripts\demo_wiki_offline.py --out "$env:USERPROFILE\wiki-demo-01"

# PR #20で準備したexperiment.jsonにWiki軸を追加（計画のみ）
python .\scripts\wiki_offline.py matrix `
  --base-suite "$env:USERPROFILE\learning-suite\experiment.json" `
  --out "$env:USERPROFILE\wiki-experiment-plan.json"
```

一般CLI: `wiki_offline.py prompt|build|verify|query`。`--mode original|agentcore --input <JSON> --input-sha256 <SHA> --project <ID> --training-sha256 <SHA> --policy <Git外辞書>`を共通指定する。
`prompt`は`--out`、`build`は`--proposal`と`--out`、`verify/query`は`--directory`と`--manifest-sha256`が必要。queryには`--query`を追加する。旧版を編集材料にする場合はprompt/buildへ`--previous <wiki.json>`を指定する。書き出されたpromptには機密になり得る本文が含まれるため、外部に自動送信しない。

## 6. 13条件×3方式の比較

既存13条件は維持し、Wikiなし/WM/WOを最初から用意する。**39条件は準備計画であり、AWSの39リソース作成や実験完了ではない。** D+W0だけが本当の知識なし対照。D+WM/WOはWikiだけを使う追加実験とする。

直接検索した記憶とWikiが使う文脈・検索件数は既存の総予算を共用する設計。WM/WOで同じ原本、同じWiki編集モデル/プロンプト/予算を使い、同一反復内では確定Wiki版を共用、反復間では分離する。評価中にWikiを更新しない。モデル出力・実行結果・費用は未実行nullのまま。編集モデルの指定と予算の実行時強制は実runnerに接続する工程で残る。

測る項目: 根拠対応率、一般化/適用精度、古い知識の利用率、矛盾検出、未知タスクの成否、Wiki構築/更新費用、検索/回答費用。単にページ数が増えたことを改善としない。

## 7. 残課題

Memoryの実作成・投入・非同期抽出完了の独立検収、実LLM生成品質と自律的な反復編集、意味的な矛盾評価、署名済みWiki採用状態と既存KnowledgeStoreとの連携、検索文脈の実提供、39条件の観測変換と実runner、実タスク比較、会社DLP/SSO/Windows検証が未完了。Memory投入/取得とモデル呼出経路、有限回再開コードは実装済みですが実AWSで未検証です。Issue #21はOPENのまま。

## 一次資料（2026-11-15確認）

- AWSの抽出/統合/Reflectionとスキーマ制約: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-custom-strategy.html
- Episodic: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/episodic-memory-strategy.html
- Memoryレコード、namespace、pagination: https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_ListMemoryRecords.html
- 元資料とWikiを分け、知識を統合/リンク/点検するパターン: https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f
