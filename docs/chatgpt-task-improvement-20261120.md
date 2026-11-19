# ChatGPT Task Improvement: 第3系統（Issue #41）

2026-11-20。既存のCopilot知識学習・Session Retrospectiveに加え、ChatGPTの業務依頼とDesktop Commanderの実行履歴を**ローカル・読取専用**で匿名集計する経路。実業務の成功判定、自動Skill適用、外部モデル呼び出しは含まない。

## 入力形式と取得方法

- ChatGPT: [OpenAI公式のデータエクスポート](https://help.openai.com/ja-jp/articles/7260999-exporting-your-chatgpt-history-and-data)で取得したZIPを利用者が安全なローカルフォルダへ展開し、JSON会話ファイルを明示的に指定する。本PoCは既知の id / mapping / current_node / message.author.role / content.parts を持つ会話のみ認識する。別分岐や非テキスト要素は取り込まない。公式があらゆる版の内部JSONスキーマを保証しているわけではないため、対応外ならエラーにする。
- Desktop Commander: ローカルの tool-history.jsonl（Windowsでは通常ユーザーホーム直下の .claude-server-commander に保存）または利用者がAPIの呼び出し履歴を保存したJSON配列を指定する。呼び出し名、明示的なエラーフラグ、長文出力の省略件数のみ使い、コマンド・引数・ツール出力はレポートへ保存しない。直近履歴は最大1,000件、長い出力は省略される場合があり、完全監査ログではない。
- 既存の offline-sensitive-literals 形式の機密語辞書を別のGit管理外ファイルとして指定する。既知のキー・メール・電話などはユーザー発言の解析を拒否する。すべての未知機密が見つかる保証はない。

## ローカルでの使い方

ローカルPCで会話JSONと履歴JSONLを用意してから実行する。入力・出力・policyはGit管理外。ユーザー本人が選んだファイルだけを扱う。

    python .\scripts\analyze_chatgpt_desktop_offline.py analyze --chatgpt-json "$env:USERPROFILE\private-export\conversations.json" --desktop-history "$env:USERPROFILE\.claude-server-commander\tool-history.jsonl" --policy "$env:USERPROFILE\private-export\policy.json" --out "$env:USERPROFILE\chatgpt-task-review-01" --acknowledge-local-analysis

片側の入力を省略しても利用可能。出力は chatgpt-task-improvement.preview.json と manifest.json で、生の会話、タイトル、コマンド、ツール結果、ファイル名は記録しない。出力した manifest_sha256 は別の場所に控えておく。

    python .\scripts\analyze_chatgpt_desktop_offline.py verify --chatgpt-json "$env:USERPROFILE\private-export\conversations.json" --desktop-history "$env:USERPROFILE\.claude-server-commander\tool-history.jsonl" --policy "$env:USERPROFILE\private-export\policy.json" --out "$env:USERPROFILE\chatgpt-task-review-01" --acknowledge-local-analysis --expected-manifest-sha256 "<控えたmanifest SHA>"

## 検出する候補

| 兆候 | 検討する改善先 |
|---|---|
| ユーザーが完了報告を訂正 | ChatGPT Projectの完了条件・外部の検収 |
| 依頼のやり直しや指示の不一致 | 目的・制約・検証方法を含む依頼文テンプレート |
| Desktop Commanderが明示的にエラーを返す | 操作手順と失敗時の原因診断 |
| read_process_outputが多い | 無駄な再試行なのか適切なプロセス監視なのかを人が確認 |
| 2系統の情報がある | 今後は共通Task IDとGitHub・正式成果物のIDを付与する |

共通IDや独立の成果物証拠がないため、**会話とツール呼び出しを時刻だけで紐づけない**。エラーフラグがない呼び出しはunknownであり、成功と判断しない。兆候は因果関係・業務完了の証明ではない。

## 安全・制約

- ローカルで手動選択したファイルのみ読み取り。公式データエクスポートのダウンロード自体は自動化しない
- Git管理外の新規フォルダへしか出力できず、既存ファイルは上書きしない
- 会話本文・コマンド/引数・ツール出力はGitHub公開リポジトリ、モデル、AWSへ送信しない
- 将来、意味的なLLMレビューを接続する場合は利用許可・DLP・独立モデル評価が必要
- ChatGPT Business/Enterprise等のデータ利用は組織の管理規程を確認
- Windowsでの実セッション接続、全エクスポート版との互換性、実業務の改善効果は未検証
- GitHub Actions/CIとCopilotファイル自動変更を追加しない。既存のAWS通信停止も維持する

## 合成テスト

    python -m unittest discover -s tests -p test_chatgpt_task_trace.py -v

合成テストPASSは実業務の達成や機密検出の完全性を保証しない。
