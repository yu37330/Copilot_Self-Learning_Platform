# Issue #31：2系統共通の生成・独立評価モデル契約（オフライン）

2026-11-20。対象は（1）既存のNote・Skill知識学習系、（2）Session RetrospectiveによるAGENTS.md・Skills改善系。両方で「候補生成→既存の安全ルール→別モデルによる独立レビュー」という共通の型を用意した。

**重要：今回のコードは外部LLMを呼ばない。** モデルが生成したと仮定するローカルJSONを、要求system+promptのSHAと照合して再生する実装。実際のモデルID、実際の品質、送信承認の真正性を保証しない。AWS/Bedrock/OpenAI/GitHub Copilotへの通信や自動Skills適用はない。

## 実装

- copilot_learning/offline_model_port.py：JSONModelPort、ModelRoute、OfflineBoundJSONModel。異なるモデルIDと役割を必須にし、要求hashが一致しなければ拒否。1応答を再利用しない。
- copilot_learning/dual_track_review.py：右はPhase A検出→Phase Bの構造・証拠・指示版SHA検査→別のモデル意見→未適用diff。左は既存のgenerate_candidates、grade_candidates、independent_llm_gradeを二役割で実行するオフライン再生。
- copilot_learning/learning.py：既存Grader入力をindependent_grade_promptとして公開。入力データ自体は従来のものから変更しない。
- scripts/dual_track_review_offline.py：generator-request、reviewer-request、reviewの3コマンド。
- tests/test_dual_track_review.py、tests/test_dual_track_review_cli.py：合成の根拠・指示版・モデルID・失敗安全性をテスト。

## 能力と制限

| 段階 | 実装済み | 未完成 |
|---|---|---|
| モデル役割の分離 | 別のモデルID・役割を要求 | 実モデルIDの認証・別ベンダー実接続 |
| 左：知識学習 | 二役割で既存の生成/Grade入力を再生 | 実LLMの学習品質検証 |
| 右：指示改善 | 兆候→応答JSON→証拠・SHA・ルール検査→独立の意見→diff | LLMによる根本原因の自律分析 |
| レビューと採用 | 意見がhold/rejectなら全diffを抑止 | 本物の独立モデル・人間の採用承認 |
| 効果測定 | 既存のPhase C評価器あり | 実タスクの改善効果検証 |

右の生成プロンプトには元会話の本文・既存指示全文が入らない。兆候と証拠ID・指示ファイルSHAだけでは意味的な因果関係・矛盾を実証できないので、レビューにagreeがあってもpending_manual_review。これは自動承認されない。左の従来Grader用データには生会話の原文が含まれ得るので、公開Gitや未承認APIへ送ってはいけない。

## 操作（PowerShell）

機密語辞書をGit管理外に置き、原本セッションの利用許可を確認。下記の出力先は全てGit管理外の新規フォルダを指定する。

1. 生成用の構造化リクエストを作成（モデルは実行しない）。

~~~powershell
python .\scripts\dual_track_review_offline.py generator-request --staging "$env:USERPROFILE\copilot-staging" --fingerprint "64桁fingerprint" --repo "C:\path\to\repository" --policy "$env:USERPROFILE\copilot-private\policy.json" --out "$env:USERPROFILE\copilot-private\generator-request-01"
~~~

2. 生成側の持込み応答をPhase Bで検査し、独立レビュー用の構造化リクエストを作成する（モデルは実行しない）。

~~~powershell
python .\scripts\dual_track_review_offline.py reviewer-request --staging "$env:USERPROFILE\copilot-staging" --fingerprint "64桁fingerprint" --repo "C:\path\to\repository" --policy "$env:USERPROFILE\copilot-private\policy.json" --proposal "$env:USERPROFILE\copilot-private\proposal.json" --out "$env:USERPROFILE\copilot-private\reviewer-request-01"
~~~

3. 別途得た独立レビューJSONも渡して、未適用の差分プレビューを生成する。

~~~powershell
python .\scripts\dual_track_review_offline.py review --staging "$env:USERPROFILE\copilot-staging" --fingerprint "64桁fingerprint" --repo "C:\path\to\repository" --policy "$env:USERPROFILE\copilot-private\policy.json" --proposal "$env:USERPROFILE\copilot-private\proposal.json" --opinion "$env:USERPROFILE\copilot-private\opinion.json" --generator-model-id offline-gen-01 --reviewer-model-id offline-review-02 --out "$env:USERPROFILE\copilot-private\dual-review-01"
~~~

出力はdual-review.preview.json、changes.preview.diff、manifest.json。生成・評価モデルIDは異なる文字列を求めるが、申告文字列の真正性は未検証。

## 独立評価の形式

~~~json
{
  "schema_version": "1.0",
  "record_type": "offline-retro-opinion",
  "proposal_sha256": "提案JSONのSHA256（json_bytes）",
  "inventory_sha256": "指示一覧SHA256",
  "decisions": [
    {
      "proposal_index": 0,
      "decision": "hold",
      "reason": "Existing instructions were not supplied, so semantic overlap remains unknown."
    }
  ]
}
~~~

評価側は全提案への意見が必要。index/件数/SHAが違えば拒否、hold/rejectが一件でもあればdiff全体を空にする。agreeでも実モデル承認ではなく人間のレビュー待ち。

## 必要な次工程

- 本物のモデル接続（Bedrock、OpenAI等）。送信承認・機密/DLP・モデルID固定・認証・タイムアウト・利用料金・ログ検査が必要。既存のオンライン安全停止は解除しない。
- 実セッション・既存指示の安全な意味解析、独立したセキュリティ評価。
- 実Copilotで変更前後を比較するrunner、未学習評価、採用前レビュー。
- Issue #34のWindows既存11エラーの解消、Issue #36の実Copilotデータ形式受入。

## 合成テスト

~~~powershell
python -m unittest discover -s tests -p "test_dual_track_review*.py" -v
~~~

モデル実行はゼロ、GitHub Actions/CI・自動インストール・外部送信なし。
