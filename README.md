# AutoPrompter

指定時刻に Claude Code セッションを自動起動するか、すでに動いている
tmux の Claude Code/Codex へプロンプトを投入するツール。
レートリミットが回復するタイミングに仕事を仕込んでおき、枠を無駄にしないための道具。

セッションは tmux の中で起動し、応答後も生きたまま残る。
後から `autoprompt attach` で合流して、そのまま会話を続けられる。

## 仕組み

```text
新規セッション:
launchd → runner.sh → tmux new-session → claude <プロンプト>

既存tmux:
launchd → runner.sh → 対象paneの同一性・入力待ちを確認 → promptをpaste
```

新規セッションではプロンプトを `claude` の位置引数として渡す。
既存tmuxでは、対象paneが予約時と同じプロセスで、画面が静止し、最後の有効行が
空の入力欄である場合だけtmux bufferから貼り付ける。生成中、入力途中、確認画面、
shell待機中、別プロセスへ変化した場合は安全のため送信しない。

## セットアップ

`autoprompt` に PATH を通す。`~/.zshrc` に以下を追記:

```bash
export PATH="$PATH:/Users/masatoshibukawa/Documents/03_Scenario_based_HDDP/Scenario_based_HDDP_worktrees/AutoPrompter"
```

反映は `source ~/.zshrc` か、ターミナルを開き直す。

## 新しいClaude Codeセッションを起動する

### 1. ジョブを書く

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

## 既存tmuxへプロンプトを投入する

### 対象paneを確認する

```bash
tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index}  #{pane_id}'
```

`hddp:0.0` は「`hddp`セッションのwindow 0、pane 0」という意味。

### 今すぐ投入する

```bash
autoprompt send hddp:0.0 "続きを実行してください"
```

長文や機密情報を含むプロンプトは、shell履歴へ本文を残さないようファイルを使う。

```bash
autoprompt send hddp:0.0 --prompt-file /path/to/prompt.txt
```

通常は、対象がClaude Code/Codexの空の入力待ちであると確認できた場合だけ送信する。
状態を確認できない場合は何も送らずエラーになる。

```bash
autoprompt send hddp:0.0 --prompt-file /path/to/prompt.txt --force
```

`--force`は入力欄を消して置き換える。shell上ではプロンプトがコマンドとして
実行される危険もあるため、対象paneを目視確認した場合だけ使用すること。

### 指定時刻に投入する

TOMLでは`cwd`の代わりに`tmux_target`を指定する。

```toml
name = "hddp-existing"
tmux_target = "hddp:0.0"
prompt_file = "/path/to/prompt.txt"
on_rate_limit = "none"
```

```bash
autoprompt add /path/to/job.toml --at "03:00"
```

登録時に対象を一意なpane IDへ解決し、PID、起動コマンド、現在のコマンド、
session ID、window IDを保存する。実行時に1つでも変化していれば送信しない。
予約時と実行時の両方でshell待機中のpaneを拒否する。

既存tmuxの起動設定を引き継ぐため、`cwd`、`model`、`effort`、
`permission_mode`は新たに適用されない。またwatchdogによる切り替えは対象外なので、
`on_rate_limit = "none"`だけを使用できる。

詳しい安全仕様は[TMUX_USAGE.txt](TMUX_USAGE.txt)にもまとめている。

## AutoPrompter管理セッションを続ける

Plan Max などで実行中にレートリミットに引っかかって止まったセッションは、
tmux の中で生きたまま待機している。そこに続行プロンプトを投げる:

```bash
autoprompt continue dip_check                # 「続けて」を送る
autoprompt continue dip_check "続けて、特に○○に注意して"  # 独自の文言
```

tmux セッションが既に終了している場合は使えない。その場合は
`claude --continue` で新しいプロセスを起こして履歴から再開すること
(セッションが tmux ごと消えている前提なので、AutoPrompter の外の操作になる)。

## レートリミットを自動検知して対応する (watchdog)

「止まったのを見て手動で continue する」を、さらに自動化する機能。
ジョブ TOML に `on_rate_limit` を指定したジョブだけが対象になる。

```toml
on_rate_limit = "codex"      # none(既定) / continue / codex
rate_limit_threshold = 95    # 使用率何%で動き出すか(既定95)
```

| 値              | 動作                                                                                                                                                                    |
| --------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `none` (既定) | 何もしない。今まで通り手動で`continue` する                                                                                                                           |
| `continue`    | 閾値を超えて実際にセッションが止まっていたら、ログに記録する。実際の続行はまさとが手動で`autoprompt continue` する                                                    |
| `codex`       | 同上に加えて、続行を試みても応答がなければ**Codex に引き継ぐ**。同じ tmux セッションの中で `claude` を終え、直前の画面の抜粋を渡した状態で `codex` を起動する |

監視デーモンの起動・停止:

```bash
autoprompt watchdog-start   # 5分おきの巡回を開始 (launchd に登録)
autoprompt watchdog-status  # 稼働状況と現在の使用率を確認
autoprompt watchdog-stop    # 停止
```

**`autoprompt add` 時の自動起動**: `on_rate_limit` を `none` 以外にした
ジョブを `add` すると、watchdog がまだ起動していなければ自動で起動する。
TOML に `on_rate_limit = "codex"` などと書いたのに watchdog 本体が
未起動で引き継ぎが発火しない、という取りこぼしを防ぐための挙動。
明示的に `watchdog-start` を打つ必要はない(打っても害はない)。

**動作の仕組み**: 5分おきに `~/.claude.json` の実際の使用率を見て、閾値を
超えていて、かつ対象ジョブの tmux セッションが**本当に静止している**
(数秒間画面が一切変化しない)場合だけ動く。まず軽い試し打ちを送り、
応答があれば「リミットはまだ生きている」と判断してそこで止まる。
応答が無ければ `on_rate_limit` の設定に従う。

**閾値を95%にしている理由**: 100%ギリギリまで待つと、Codexへの引き継ぎ
処理そのものを実行するトークンすら残っていない可能性がある。かといって
閾値を下げすぎると、まだ十分使える枠を早々と手放すことになる。
「95%で試し打ち、ダメならすぐ最小構成で切り替える」という2段構えに
することで、閾値の精密さそのものへの依存を減らしている。

**やらないこと**: 信頼確認ダイアログなど、レートリミット以外の理由で
セッションが止まっている場合は判別できないため、何もしない。
ダイアログを自動で承認・突破する処理は一切実装していない
(セキュリティ上、意図的に入れていない)。判別できない停止は
まさとが `autoprompt attach` して自分の目で見て判断すること。

**一度対応したジョブは再度触らない**: 試し打ちを送った後は
`~/Library/Application Support/AutoPrompter/state/<name>.watchdog.json`
に対応履歴が残り、同じジョブに二重に対応することはない。
再度対象にしたい場合はこのファイルを削除する。

## その他のコマンド

```bash
autoprompt cancel dip_check  # 予約を取り消す
autoprompt kill   dip_check  # tmux セッションを終了する
autoprompt run    jobs/x.toml # 予約せず即実行 (動作確認用)
```

## 設定できる項目

| 項目                         | 必須         | 値                                                                                       |
| ---------------------------- | ------------ | ---------------------------------------------------------------------------------------- |
| `name`                     | -            | ジョブ名。省略時はファイル名。英数字・ハイフン・アンダースコアのみ                       |
| `cwd`                      | 新規時必須   | 新規Claudeセッションの作業ディレクトリ。`~` 展開あり                                   |
| `tmux_target`              | 既存時必須   | 既存tmuxの対象。例:`hddp:0.0`。指定時は`cwd`不要                                     |
| `prompt` / `prompt_file` | どちらか必須 | 投入するプロンプト                                                                       |
| `model`                    | -            | `opus` / `sonnet` / `haiku` / `fable`、または `claude-opus-5` 等               |
| `effort`                   | -            | `low` / `medium` / `high` / `xhigh` / `max`                                    |
| `permission_mode`          | -            | `manual` / `plan` / `auto` / `acceptEdits` / `dontAsk` / `bypassPermissions` |
| `auto_decide`              | -            | `true` にすると、選択肢を提示せず推奨案で進めるようプロンプトに自動で付け足す          |
| `on_rate_limit`            | -            | `none`(既定) / `continue` / `codex`。レートリミット自動対応の挙動。詳細は下記      |
| `rate_limit_threshold`     | -            | `on_rate_limit` が動き出す5時間枠の使用率(%)。既定 95                                  |

`cwd`と`tmux_target`は動作モードを決める項目。新しいClaude Codeを起動するなら`cwd`、
すでに動いているtmuxへ送るなら`tmux_target`を使う。

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

プロンプトの一時スナップショットは作成時から権限`0600`とし、runnerの成功・失敗・
安全判定による中断のいずれでも削除する。実行可能なrunner本体へプロンプト本文は埋め込まない。

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

**既存tmuxへの投入が拒否される**

```bash
tmux capture-pane -p -t hddp:0.0 | tail -30
tmux display-message -p -t hddp:0.0 '#{pane_current_command}'
```

生成中、入力途中、確認ダイアログ、shell待機中、または予約後にpaneのプロセスが
変わった場合は意図的に拒否する。作業完了後にもう一度実行するか、対象を確認して
予約を登録し直す。

## レートリミット回復時刻の読み取り方(参考)

watchdog は `~/.claude.json` の `cachedUsageUtilization` を使っている。
サーバが返す実際の値なので、「到達時刻 + 5時間」のような推定は不要。

- `~/.claude.json` の `cachedUsageUtilization.utilization.five_hour.resets_at` (ISO8601, 5分TTL)
- statusLine フックに渡される JSON の `rate_limits.five_hour.resets_at` (Unix秒, 対話セッション中のみ、watchdog は未使用)

注意: `~/.claude.json` は設定本体。読み取り専用で扱うこと (書き込むと全設定が壊れる)。

## 今後の拡張余地

- watchdog は現状 AutoPrompter が起こしたジョブのみが対象。まさとが
  普段使っている VSCode 内のメインセッションは tmux の外で動いているため、
  同じ方式では対応できない(別の実現方法が必要)
- 信頼確認ダイアログ等、レートリミット以外の理由での停止検知は未対応
