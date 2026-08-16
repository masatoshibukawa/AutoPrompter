# AutoPrompter

指定時刻に Claude Code セッションを自動起動し、プロンプトを投入するツール。
レートリミットが回復するタイミングに仕事を仕込んでおき、枠を無駄にしないための道具。

セッションは tmux の中で起動し、応答後も生きたまま残る。
後から `autoprompt attach` で合流して、そのまま会話を続けられる。

## 仕組み

```
launchd (指定時刻に発火)
   └─> runner.sh
         └─> tmux new-session -d -s ap-<ジョブ名>
               └─> cd <cwd> && claude --model .. --effort .. "<プロンプト>"
                     (応答後もセッションは生存 → 後から attach)
```

プロンプトは `claude` の位置引数として渡している。
キー入力の模擬 (`tmux send-keys`) は使っていないので、
起動タイミングやダイアログに影響されず確実に投入される。

## セットアップ

`autoprompt` に PATH を通す。`~/.zshrc` に以下を追記:

```bash
export PATH="$PATH:/Users/masatoshibukawa/Documents/03_Scenario_based_HDDP/Scenario_based_HDDP_worktrees/AutoPrompter"
```

反映は `source ~/.zshrc` か、ターミナルを開き直す。

## 使い方を書く

`jobs/` に TOML を置く。[jobs/example.toml](jobs/example.toml) が雛形。

```toml
name   = "dip_check"
cwd    = "~/Documents/03_Scenario_based_HDDP/Scenario_based_HDDP_worktrees/SDDP_ver4.1"
model  = "opus"
effort = "high"
prompt = """
/goal DIP の t=30 が収束すること
resampling.jl の keep_rows まわりを調査してほしい。
"""
```

プロンプトを別ファイルにしたい場合は `prompt` の代わりに:

```toml
prompt_file = "./prompts/dip_investigation.md"
```

### 2. 予約する

```bash
autoprompt add jobs/dip_check.toml --at "03:00"
```

`--at` の書き方:

| 書き方                 | 意味                              |
| ---------------------- | --------------------------------- |
| `"03:00"`            | 次に来る 03:00 (過ぎていれば翌日) |
| `"2026-08-17 03:00"` | 絶対時刻                          |
| `"+90m"` / `"+2h"` | 今から何分/何時間後               |

### 3. 確認・合流する

```bash
autoprompt list              # 予約と実行中セッションの一覧
autoprompt attach dip_check  # セッションに合流して会話を続ける
```

`attach` した後、抜けるには `Ctrl-b d` (tmux のデタッチ)。
セッションは生きたままなので、また `attach` すれば戻れる。

### レートリミットで止まったセッションを続ける

Plan Max などで実行中にレートリミットに引っかかって止まったセッションは、
tmux の中で生きたまま待機している。そこに続行プロンプトを投げる:

```bash
autoprompt continue dip_check                # 「続けて」を送る
autoprompt continue dip_check "続けて、特に○○に注意して"  # 独自の文言
```

tmux セッションが既に終了している場合は使えない。その場合は
`claude --continue` で新しいプロセスを起こして履歴から再開すること
(セッションが tmux ごと消えている前提なので、AutoPrompter の外の操作になる)。

### その他のコマンド

```bash
autoprompt cancel dip_check  # 予約を取り消す
autoprompt kill   dip_check  # tmux セッションを終了する
autoprompt run    jobs/x.toml # 予約せず即実行 (動作確認用)
```

## 設定できる項目

| 項目                         | 必須         | 値                                                                                       |
| ---------------------------- | ------------ | ---------------------------------------------------------------------------------------- |
| `name`                     | -            | ジョブ名。省略時はファイル名。英数字・ハイフン・アンダースコアのみ                       |
| `cwd`                      | 必須         | 作業ディレクトリ。`~` 展開あり                                                         |
| `prompt` / `prompt_file` | どちらか必須 | 投入するプロンプト                                                                       |
| `model`                    | -            | `opus` / `sonnet` / `haiku` / `fable`、または `claude-opus-5` 等               |
| `effort`                   | -            | `low` / `medium` / `high` / `xhigh` / `max`                                    |
| `permission_mode`          | -            | `manual` / `plan` / `auto` / `acceptEdits` / `dontAsk` / `bypassPermissions` |
| `auto_decide`              | -            | `true` にすると、選択肢を提示せず推奨案で進めるようプロンプトに自動で付け足す          |

## 知っておくべき挙動

以下はすべて実測で確認した Claude Code の挙動。

### permission_mode = "plan" は model 指定を上書きする

プランモードは計画立案のため、Claude Code が内部で上位モデルに切り替える。
`model = "haiku"` と書いても実際には Sonnet で走る。
指定したモデルで走らせたいなら `permission_mode` を外すこと。
両方書いた場合は登録時に警告が出る。

### 無人実行と permission_mode

既定 (`manual`) では Claude が確認を求めるので、まさとが見ていない間は作業が進まない。
「起動とプロンプト投入だけ自動化し、朝 attach してから承認する」なら既定のままでよい。

`acceptEdits` / `dontAsk` / `bypassPermissions` は、
**見ていない間に無確認でファイル変更やコマンド実行が行われる**ことを意味する。
登録時に警告は出るが、止めはしない。リスクを理解した上で選ぶこと。

### 信頼済みディレクトリ

未承認のディレクトリで起動すると信頼確認ダイアログで止まり、ジョブが空振りする。
`autoprompt add` は登録時にこれを検査して警告する。
警告が出たら、先に一度手動で `cd <dir> && claude` を実行して承認しておくこと。

### auto_decide の効果範囲

`auto_decide = true` は「選択肢を出さず自分で決めて進めて」という指示を
プロンプト末尾に付け足すだけで、Claude Code の UI 上の選択ダイアログを
自動でクリックする仕組みではない。Claude 自身の判断で選択肢の提示を
避けてもらう形なので、状況によっては選択肢が出ることもありうる。
出た場合はそこで止まるので、無人実行では起きてほしくない作業には向かない。

### effort のタイポは静かに無視される

`claude` 本体は不正な `--effort` 値を警告だけ出して既定値に落とす。
そのため `autoprompt` 側で事前に検証してエラーにしている。

## ファイルの置き場所

| 場所                                                     | 中身                          |
| -------------------------------------------------------- | ----------------------------- |
| `AutoPrompter/jobs/`                                   | ジョブ定義 (まさとが編集する) |
| `~/Library/Application Support/AutoPrompter/state/`    | 自動生成の runner と状態      |
| `~/Library/Application Support/AutoPrompter/logs/`     | 実行ログ                      |
| `~/Library/LaunchAgents/com.masato.autoprompt.*.plist` | 予約 (launchd)                |

runner とログを `~/Library` 配下に置いているのは macOS の TCC 対策。
launchd 経由のプロセスは `~/Documents` 配下のファイルを読めないため
(実測: exit 127 `can't open input file`)。

## トラブルシューティング

**予約時刻になっても何も起きない**

```bash
# launchd に登録されているか
launchctl print "gui/$(id -u)/com.masato.autoprompt.<ジョブ名>" | grep -E 'runs|last exit'

# runner のログ
cat ~/Library/Application\ Support/AutoPrompter/logs/<ジョブ名>.log
```

`last exit code = 0` かつログに「起動成功」があれば、セッションは立っている。
`autoprompt attach <ジョブ名>` で確認する。

**セッションに入れない**

```bash
tmux ls | grep ap-    # AutoPrompter のセッション一覧
```

セッションが無ければ既に終了している。ログを確認する。

## 今後の拡張余地

レートリミット回復時刻は、以下から読み取れることを確認済み。
これを使えば「リミットが回復したら自動投入」も実装できる。

- `~/.claude.json` の `cachedUsageUtilization.utilization.five_hour.resets_at` (ISO8601, 5分TTL)
- statusLine フックに渡される JSON の `rate_limits.five_hour.resets_at` (Unix秒, 対話セッション中のみ)

サーバが返す実際の値なので、「到達時刻 + 5時間」のような推定は不要。

注意: `~/.claude.json` は設定本体。読み取り専用で扱うこと (書き込むと全設定が壊れる)。
