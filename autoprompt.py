#!/usr/bin/env python3
"""AutoPrompter - 指定時刻に Claude Code セッションを起動してプロンプトを自動投入する。

ジョブは TOML ファイルで定義し、launchd で指定時刻に発火させ、
tmux セッションの中で `claude` を対話モードで起動する。
セッションは実行後も生存するので、後から attach して会話を引き継げる。
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
import uuid
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
JOBS_DIR = BASE_DIR / "jobs"

# runner スクリプトとログは ~/Library/Application Support 配下に置く。
# macOS の TCC (プライバシー保護) により、launchd 経由のプロセスは
# ~/Documents 配下のファイルを読めない (実測: exit 127 "can't open input file")。
# ~/Library 配下ならこの制限を受けない。
SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "AutoPrompter"
STATE_DIR = SUPPORT_DIR / "state"
LOG_DIR = SUPPORT_DIR / "logs"
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"

# launchd の Label と tmux セッション名に使う接頭辞。
# 既存の tmux セッション(まさとが手動で開いているもの)と衝突させないための名前空間。
LABEL_PREFIX = "com.masato.autoprompt"
TMUX_PREFIX = "ap-"

VALID_MODELS = {"opus", "sonnet", "haiku", "fable"}
VALID_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
VALID_PERMISSION_MODES = {
    "manual",
    "auto",
    "acceptEdits",
    "dontAsk",
    "bypassPermissions",
    "plan",
}
# まさとが見ていない時刻に無確認でファイル変更・コマンド実行を許す設定。
# 使用を禁止はしないが、登録時に必ず警告する。
RISKY_PERMISSION_MODES = {"bypassPermissions", "dontAsk", "acceptEdits"}

JOB_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# --- watchdog (レートリミット自動対応) ---

VALID_ON_RATE_LIMIT = {"none", "continue", "codex"}

# 5時間枠の使用率がこの値(%)以上になったら watchdog が動き出す。
# 高すぎると試し打ちすら通らないほど枠が尽きた後になるリスクがあり、
# 低すぎるとまだ余裕があるうちに Codex へ切り替えてしまう。
# 「試し打ちが失敗すれば要約なしで即 Codex に切り替える」という
# 2段構えを前提に、やや保守的な値を既定にしている。
DEFAULT_RATE_LIMIT_THRESHOLD = 95

# 何分おきに使用率とセッション状態を確認するか。
WATCHDOG_INTERVAL_MINUTES = 5

WATCHDOG_LABEL = f"{LABEL_PREFIX}.watchdog"


# ---------------------------------------------------------------------------
# 小物
# ---------------------------------------------------------------------------


def die(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"エラー: {msg}", file=sys.stderr)
    sys.exit(1)


def warn(msg: str) -> None:
    print(f"警告: {msg}", file=sys.stderr)


def ensure_dirs() -> None:
    for d in (JOBS_DIR, STATE_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)


def label_for(name: str) -> str:
    return f"{LABEL_PREFIX}.{name}"


def plist_path_for(name: str) -> Path:
    return LAUNCH_AGENTS_DIR / f"{label_for(name)}.plist"


def session_for(name: str) -> str:
    return f"{TMUX_PREFIX}{name}"


def state_path_for(name: str) -> Path:
    return STATE_DIR / f"{name}.json"


def prompt_path_for(name: str) -> Path:
    return STATE_DIR / f"{name}.prompt.txt"


def watchdog_state_path_for(name: str) -> Path:
    """ジョブごとの watchdog 対応履歴。同じ対応を繰り返さないための記録。"""
    return STATE_DIR / f"{name}.watchdog.json"


def gui_domain() -> str:
    return f"gui/{os.getuid()}"


# ---------------------------------------------------------------------------
# ジョブ定義の読み込みと検証
# ---------------------------------------------------------------------------


# auto_decide = true のとき、プロンプト末尾に付け足す指示。
# 選択肢の提示そのものを止めさせることで、無人実行中に選択待ちで
# 固まる(=まさとが朝 attach するまで進まない)事態を避ける。
AUTO_DECIDE_SUFFIX = """

---
この実行は無人で行われます。作業の途中で選択肢を提示せず、
最も推奨できる案を自分で選んで進めてください。
どれを選び、なぜそうしたかは、後で分かるように結果に明記してください。"""


class Job:
    """検証済みのジョブ定義。"""

    def __init__(self, data: dict, source: Path):
        self.source = source
        self.name: str = data["name"]
        self.cwd: Path | None = data.get("cwd")
        self.tmux_target: str | None = data.get("tmux_target")
        self.model: str | None = data.get("model")
        self.effort: str | None = data.get("effort")
        self.permission_mode: str | None = data.get("permission_mode")
        self.prompt: str = data["prompt"]
        self.auto_decide: bool = bool(data.get("auto_decide", False))
        # レートリミット時の挙動。
        #   none      … 何もしない(既定)。まさとが手動で判断する
        #   continue  … 使用率が閾値を超えたら watchdog が自動で continue を試みる
        #   codex     … continue を試みて失敗したら Codex に引き継ぐ
        self.on_rate_limit: str = data.get("on_rate_limit", "none")
        self.rate_limit_threshold: int = int(
            data.get("rate_limit_threshold", DEFAULT_RATE_LIMIT_THRESHOLD)
        )

    def prepared_prompt(self) -> str:
        return self.prompt + AUTO_DECIDE_SUFFIX if self.auto_decide else self.prompt

    def claude_argv(self, include_prompt: bool = True) -> list[str]:
        """`claude` 起動時のコマンドライン引数を組み立てる。

        -p は付けない。付けると応答後に即終了してしまい、
        「後から attach して会話を続ける」という目的が果たせなくなる。
        """
        argv = ["claude"]
        if self.model:
            argv += ["--model", self.model]
        if self.effort:
            argv += ["--effort", self.effort]
        if self.permission_mode:
            argv += ["--permission-mode", self.permission_mode]
        if include_prompt:
            argv.append(self.prepared_prompt())
        return argv


def load_job(path: Path) -> Job:
    if not path.exists():
        die(f"ジョブファイルが見つかりません: {path}")

    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        die(f"TOML の構文エラー ({path}): {e}")

    # --- name ---
    name = data.get("name") or path.stem
    if not JOB_NAME_RE.match(name):
        die(f"name は英数字・ハイフン・アンダースコアのみ使えます: {name!r}")

    # --- tmux_target / cwd ---
    raw_tmux_target = data.get("tmux_target")
    tmux_target = str(raw_tmux_target).strip() if raw_tmux_target is not None else None
    if raw_tmux_target is not None and not tmux_target:
        die("tmux_target に空文字は指定できません")
    if tmux_target and any(character in tmux_target for character in ("\n", "\r", "\0")):
        die("tmux_target に改行やNUL文字は使えません")

    raw_cwd = data.get("cwd")
    if not raw_cwd and not tmux_target:
        die("cwd は必須です (Claude を起動する作業ディレクトリ)")
    cwd = None
    if raw_cwd:
        cwd = Path(os.path.expanduser(str(raw_cwd))).resolve()
        if not cwd.is_dir():
            die(f"cwd が存在しないかディレクトリではありません: {cwd}")

    # --- prompt / prompt_file ---
    prompt = data.get("prompt")
    prompt_file = data.get("prompt_file")
    if prompt and prompt_file:
        die("prompt と prompt_file は同時に指定できません")
    if prompt_file:
        # 相対パスは TOML ファイル自身の場所からの相対として解決する。
        pf = Path(os.path.expanduser(str(prompt_file)))
        if not pf.is_absolute():
            pf = (path.parent / pf).resolve()
        if not pf.exists():
            die(f"prompt_file が見つかりません: {pf}")
        prompt = pf.read_text(encoding="utf-8")
    if not prompt or not prompt.strip():
        die("prompt もしくは prompt_file で中身のあるプロンプトを指定してください")
    prompt = prompt.strip()

    # --- model ---
    model = data.get("model")
    if model is not None:
        model = str(model)
        # フルネーム(claude-opus-5 など)も許すので、エイリアス以外は警告に留める。
        if model not in VALID_MODELS and not model.startswith("claude-"):
            warn(
                f"model={model!r} は既知のエイリアス {sorted(VALID_MODELS)} に該当しません。"
                "フルネーム指定でなければタイポの可能性があります"
            )

    # --- effort ---
    # claude 側は不正値でも警告のみでデフォルトに落ちる(静かに無視される)ので、
    # ここで弾いてタイポに気づけるようにする。
    effort = data.get("effort")
    if effort is not None:
        effort = str(effort)
        if effort not in VALID_EFFORTS:
            die(f"effort={effort!r} は無効です。有効値: {sorted(VALID_EFFORTS)}")

    # --- permission_mode ---
    permission_mode = data.get("permission_mode")
    if permission_mode is not None:
        permission_mode = str(permission_mode)
        if permission_mode not in VALID_PERMISSION_MODES:
            die(
                f"permission_mode={permission_mode!r} は無効です。"
                f"有効値: {sorted(VALID_PERMISSION_MODES)}"
            )

    # --- on_rate_limit / rate_limit_threshold ---
    on_rate_limit = data.get("on_rate_limit", "none")
    if on_rate_limit not in VALID_ON_RATE_LIMIT:
        die(
            f"on_rate_limit={on_rate_limit!r} は無効です。"
            f"有効値: {sorted(VALID_ON_RATE_LIMIT)}"
        )
    rate_limit_threshold = data.get("rate_limit_threshold", DEFAULT_RATE_LIMIT_THRESHOLD)
    try:
        rate_limit_threshold = int(rate_limit_threshold)
    except (TypeError, ValueError):
        die(f"rate_limit_threshold は整数で指定してください: {rate_limit_threshold!r}")
    if not (0 < rate_limit_threshold <= 100):
        die(f"rate_limit_threshold は 1〜100 の範囲で指定してください: {rate_limit_threshold}")
    if tmux_target and on_rate_limit != "none":
        die("tmux_target を使うジョブでは on_rate_limit='none' のみ指定できます")

    return Job(
        {
            "name": name,
            "cwd": cwd,
            "tmux_target": tmux_target,
            "model": model,
            "effort": effort,
            "permission_mode": permission_mode,
            "prompt": prompt,
            "auto_decide": data.get("auto_decide", False),
            "on_rate_limit": on_rate_limit,
            "rate_limit_threshold": rate_limit_threshold,
        },
        path,
    )


# ---------------------------------------------------------------------------
# 時刻の解釈
# ---------------------------------------------------------------------------


def parse_at(at: str) -> datetime:
    """--at の指定を次回実行時刻に変換する。

    受け付ける形式:
      "03:00"              → 次に来る 03:00 (すでに過ぎていれば翌日)
      "2026-08-17 03:00"   → 絶対時刻
      "+90m" / "+2h"       → 現在からの相対時刻
    """
    at = at.strip()
    now = datetime.now()

    m = re.fullmatch(r"\+(\d+)\s*([mh])", at, re.IGNORECASE)
    if m:
        n = int(m.group(1))
        delta = timedelta(minutes=n) if m.group(2).lower() == "m" else timedelta(hours=n)
        return (now + delta).replace(second=0, microsecond=0)

    for fmt in ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(at, fmt)
        except ValueError:
            pass

    m = re.fullmatch(r"(\d{1,2}):(\d{2})", at)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            die(f"時刻が範囲外です: {at}")
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)  # 過ぎていれば翌日ぶん
        return target

    die(
        f"--at の形式が解釈できません: {at!r}\n"
        '  例: "03:00" / "2026-08-17 03:00" / "+90m" / "+2h"'
    )


# ---------------------------------------------------------------------------
# tmux
# ---------------------------------------------------------------------------


def tmux_session_exists(session: str) -> bool:
    return (
        subprocess.run(
            ["tmux", "has-session", "-t", f"={session}"],
            capture_output=True,
        ).returncode
        == 0
    )


def tmux_resolve_target(target: str) -> str | None:
    """tmux targetを一意なpane IDへ解決する。見つからなければNone。"""
    result = subprocess.run(
        ["tmux", "display-message", "-p", "-t", target, "#{pane_id}"],
        capture_output=True,
        text=True,
    )
    pane_id = result.stdout.strip()
    if result.returncode != 0 or not re.fullmatch(r"%\d+", pane_id):
        return None
    return pane_id


def tmux_target_exists(target: str) -> bool:
    return tmux_resolve_target(target) is not None


_EMPTY_AGENT_INPUT_RE = re.compile(r"^[❯›][^\S\r\n]*$", re.MULTILINE)


def _has_empty_agent_input_at_cursor(screen: str, cursor_y: int) -> bool:
    """tmuxの現在カーソル行がClaude/Codexの空入力欄ならTrueを返す。"""
    lines = screen.splitlines()
    if cursor_y < 0 or cursor_y >= len(lines):
        return False
    return _EMPTY_AGENT_INPUT_RE.fullmatch(lines[cursor_y]) is not None


def tmux_pane_cursor_y(target: str) -> int | None:
    """pane内の0始まりカーソル行を取得する。取得できなければNone。"""
    result = subprocess.run(
        ["tmux", "display-message", "-p", "-t", target, "#{cursor_y}"],
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip()
    if result.returncode != 0 or not value.isdigit():
        return None
    return int(value)


_TMUX_IDENTITY_FORMATS = {
    "pane_id": "#{pane_id}",
    "pane_pid": "#{pane_pid}",
    "pane_start_command": "#{pane_start_command}",
    "pane_current_command": "#{pane_current_command}",
    "session_id": "#{session_id}",
    "window_id": "#{window_id}",
}

_SHELL_COMMANDS = {"zsh", "bash", "fish", "sh", "dash"}


def _is_shell_command(command: str | None) -> bool:
    if not command:
        return True
    return Path(command).name.lstrip("-") in _SHELL_COMMANDS


def tmux_target_identity(target: str) -> dict[str, str] | None:
    """予約時のpaneとprocessを識別する値を取得する。"""
    identity: dict[str, str] = {}
    for name, tmux_format in _TMUX_IDENTITY_FORMATS.items():
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", target, tmux_format],
            capture_output=True,
            text=True,
        )
        value = result.stdout.strip()
        allows_empty = name == "pane_start_command"
        if result.returncode != 0 or (not value and not allows_empty):
            return None
        identity[name] = value
    if not re.fullmatch(r"%\d+", identity["pane_id"]):
        return None
    if not identity["pane_pid"].isdigit():
        return None
    if not re.fullmatch(r"\$\d+", identity["session_id"]):
        return None
    if not re.fullmatch(r"@\d+", identity["window_id"]):
        return None
    if _is_shell_command(identity["pane_current_command"]):
        return None
    return identity


def tmux_target_is_idle(target: str, samples: int = 3, interval: float = 2.0) -> bool:
    """画面が静止し、Claude/Codexの入力欄が空ならTrueを返す。"""
    previous_screen: str | None = None
    previous_cursor_y: int | None = None
    for sample_index in range(samples):
        screen = tmux_capture(target)
        cursor_y = tmux_pane_cursor_y(target)
        if cursor_y is None:
            return False
        if previous_screen is not None and (
            screen != previous_screen or cursor_y != previous_cursor_y
        ):
            return False
        previous_screen = screen
        previous_cursor_y = cursor_y
        if sample_index < samples - 1:
            time.sleep(interval)
    return (
        previous_screen is not None
        and previous_cursor_y is not None
        and _has_empty_agent_input_at_cursor(previous_screen, previous_cursor_y)
    )


def _tmux_prompt_text(prompt: str) -> str:
    return "\\\n".join(prompt.split("\n"))


def tmux_send_prompt(session: str, prompt: str) -> None:
    """生きている tmux セッションの入力欄にプロンプトを投入して送信する。

    複数行を生の改行のまま送ると Claude Code は1行ごとに別メッセージとして
    送信してしまう(実測確認済み)。Claude Code の入力欄はソフト改行に
    "\\<改行>" を使う仕様なので、末尾以外の改行をそれに置き換えてから
    load-buffer + paste-buffer で流し込む。
    """
    import tempfile

    literal_prompt = _tmux_prompt_text(prompt)
    buffer_name = f"autoprompt-{os.getpid()}-{uuid.uuid4().hex}"

    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", encoding="utf-8", delete=False
    ) as f:
        f.write(literal_prompt)
        buf_path = f.name

    buffer_loaded = False
    try:
        # 入力欄を確実に空にしてから貼り付ける。
        subprocess.run(["tmux", "send-keys", "-t", session, "C-u"], check=True)
        subprocess.run(
            ["tmux", "load-buffer", "-b", buffer_name, buf_path], check=True
        )
        buffer_loaded = True
        subprocess.run(
            ["tmux", "paste-buffer", "-d", "-b", buffer_name, "-t", session],
            check=True,
        )
        buffer_loaded = False
        # 貼り付けの描画待ち。実測で必要だった待ち時間。
        time.sleep(1)
        subprocess.run(["tmux", "send-keys", "-t", session, "Enter"], check=True)
    finally:
        if buffer_loaded:
            subprocess.run(
                ["tmux", "delete-buffer", "-b", buffer_name],
                check=False,
                capture_output=True,
            )
        Path(buf_path).unlink(missing_ok=True)


def tmux_capture(session: str) -> str:
    r = subprocess.run(
        ["tmux", "capture-pane", "-p", "-t", session],
        capture_output=True,
        text=True,
    )
    return r.stdout


def tmux_pane_current_command(target: str) -> str | None:
    result = subprocess.run(
        ["tmux", "display-message", "-p", "-t", target, "#{pane_current_command}"],
        capture_output=True,
        text=True,
    )
    command = result.stdout.strip()
    return command if result.returncode == 0 and command else None


# --- セッション状態の判別(実測ベース) ---
#
# 応答中は「記号 + 動詞 + for Ns / for 1m Ns / …」という形式のスピナー行が出る。
# 例: "✻ Crunched for 13s" "✽ Warping…"。記号・動詞はランダムに変わるため
# 固定文字列ではなく形式でマッチする。長い本文出力でスピナー行が画面外に
# 押し出されることがあるため、これ単体で「無ければアイドル」とは断定しない。
#
# 行末は [ \t]*$ で改行を跨がないようにしている。\s*$ にすると
# tmux capture-pane が返す末尾の空行の連なりに引きずられて、
# スクロールバッファの上の方に残った「過去の」スピナー行まで
# マッチしてしまう(実測で確認した誤判定の原因)。
_SPINNER_RE = re.compile(
    r"^[✻✢✽✶·]\s.*(…|for \d+(s|m \d+s))[ \t]*$", re.MULTILINE
)

# claude 本体が内部で使っているのと同じキーフレーズ(strings調査で確認)。
_RATE_LIMIT_RE = re.compile(
    r"usage limit reached|usage credit limit|out of usage credits|"
    r"close to your.*usage limit|Approaching.*usage limit",
    re.IGNORECASE,
)


def tmux_session_state(session: str, screen: str | None = None) -> str:
    """tmux セッションの現在状態を推定する(単発サンプルのみで判定)。

    戻り値: "generating"(応答中) / "rate_limited"(リミット表示あり) /
            "idle"(入力待ちらしい) / "unknown"(判別つかず)

    注意: 応答完了直後は「✻ Brewed for 5s」のようなスピナー行の名残が
    画面に残ったまま入力欄が空になる(実測で確認)。この関数はスピナー行の
    "有無" しか見ないため、名残と本当に生成中の状態を区別できない。
    区別が要る場面では tmux_session_is_idle() を使うこと。

    信頼確認ダイアログ等、レートリミット以外の理由で止まっている場合の
    区別も付けない(= "unknown" になりうる)。watchdog はそれを検知しても
    何もせず、まさとの判断に委ねる方針にしている。ダイアログを自動で
    閉じたり承認したりする処理はここには一切含めない。
    """
    if screen is None:
        screen = tmux_capture(session)

    if _RATE_LIMIT_RE.search(screen):
        return "rate_limited"
    if _SPINNER_RE.search(screen):
        return "generating"
    if "❯" in screen:
        return "idle"

    return "unknown"


def tmux_session_is_idle(session: str, samples: int = 3, interval: float = 2.0) -> bool:
    """複数回サンプリングして、確実にアイドル(入力待ち)と言えるかを判定する。

    単発の tmux_session_state() だけでは2種類の誤判定がありうる
    (いずれも実測で確認済み):
      1. スピナー出現前の一瞬や出力の切れ目で "idle" と早合点する
      2. 応答完了直後、スピナー行の名残(秒数が止まったまま)が残っていて
         "generating" と誤認し続ける

    どちらも「画面が完全に静止しているか」を見れば解決する。応答中は
    スピナーが動くか本文が流れるかで必ず capture-pane の出力が変わり続け、
    名残のスピナー行は数字も含めて完全に固定される。そのため判定基準は
    「レートリミット表示が無い」かつ「画面が samples 回・interval 秒間隔で
    一切変化しない」に統一する(スピナー行の有無そのものでは判定しない)。
    """
    prev_screen: str | None = None
    for _ in range(samples):
        screen = tmux_capture(session)
        if _RATE_LIMIT_RE.search(screen):
            return False
        if prev_screen is not None and screen != prev_screen:
            return False
        prev_screen = screen
        time.sleep(interval)
    return prev_screen is not None and "❯" in prev_screen


# ---------------------------------------------------------------------------
# 信頼済みディレクトリの確認
# ---------------------------------------------------------------------------


def is_trusted_dir(cwd: Path) -> bool | None:
    """cwd が Claude Code の信頼済みディレクトリか調べる。

    未信頼だと起動時に確認ダイアログで止まり、無人実行が失敗する。
    ~/.claude.json は設定本体なので読むだけ。絶対に書き込まない。

    戻り値: True=信頼済み / False=未信頼 / None=判定不能
    """
    cfg = Path.home() / ".claude.json"
    if not cfg.exists():
        return None
    try:
        with cfg.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    projects = data.get("projects")
    if not isinstance(projects, dict):
        return None

    # cwd 自身から順に親を辿る。Claude Code は上位ディレクトリで承認済みなら
    # その配下も信頼済みとして扱うため、自分のエントリだけ見ると誤検出する。
    for d in (cwd, *cwd.parents):
        entry = projects.get(str(d))
        if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted"):
            return True

    # どの階層にも承認記録がない。cwd 自身のエントリすら無ければ完全に未知。
    return False


# ---------------------------------------------------------------------------
# レートリミット使用率の取得
# ---------------------------------------------------------------------------


def five_hour_utilization() -> tuple[float, datetime] | None:
    """5時間枠の使用率(%)と回復時刻を返す。

    ~/.claude.json の cachedUsageUtilization を読む。サーバが返した実際の値で、
    CLI が起動している間は5分TTLで更新される。設定本体なので読むだけで、
    絶対に書き込まない。

    戻り値: (使用率0-100, 回復時刻) / 取得できなければ None
    """
    cfg = Path.home() / ".claude.json"
    if not cfg.exists():
        return None
    try:
        with cfg.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    try:
        five_hour = data["cachedUsageUtilization"]["utilization"]["five_hour"]
        utilization = float(five_hour["utilization"])
        resets_at = datetime.fromisoformat(five_hour["resets_at"])
    except (KeyError, TypeError, ValueError):
        return None

    return utilization, resets_at


# ---------------------------------------------------------------------------
# runner スクリプトの生成
# ---------------------------------------------------------------------------


def _write_private_text(path: Path, text: str) -> None:
    """作成時点から0600の一時ファイルを書き、対象pathへ原子的に置換する。"""
    import tempfile

    file_descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as file:
            file.write(text)
        temporary_path.replace(path)
    except BaseException:
        try:
            os.close(file_descriptor)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise


def _write_existing_tmux_runner(job: Job, identity: dict[str, str]) -> Path:
    """既存tmux paneへ安全に予約投入するrunnerを書き出す。"""
    runner = STATE_DIR / f"{job.name}.runner.sh"
    log_file = LOG_DIR / f"{job.name}.log"
    prompt_snapshot = prompt_path_for(job.name)
    _write_private_text(prompt_snapshot, _tmux_prompt_text(job.prepared_prompt()))

    identity_names = (
        "pane_id",
        "pane_pid",
        "pane_start_command",
        "pane_current_command",
        "session_id",
        "window_id",
    )
    expected_identity = "|".join(identity[name] for name in identity_names)
    quoted_target = shlex.quote(identity["pane_id"])
    quoted_expected_identity = shlex.quote(expected_identity)
    quoted_log = shlex.quote(str(log_file))
    quoted_state = shlex.quote(str(state_path_for(job.name)))
    quoted_prompt = shlex.quote(str(prompt_snapshot))

    script = f"""#!/bin/zsh -l
# AutoPrompter existing tmux runner (自動生成 - 直接編集しないこと)
# ジョブ: {job.name}
set -u

LOG={quoted_log}
STATE={quoted_state}
TARGET={quoted_target}
EXPECTED_IDENTITY={quoted_expected_identity}
PROMPT={quoted_prompt}
BUFFER=autoprompt-{job.name}-$$

cleanup_artifacts() {{
  tmux delete-buffer -b "$BUFFER" >/dev/null 2>&1 || true
  rm -f "$PROMPT"
}}
trap cleanup_artifacts EXIT

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 既存tmux投入開始: {job.name}" >> "$LOG"

if ! command -v tmux >/dev/null 2>&1; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 失敗: tmux が見つかりません" >> "$LOG"
  exit 1
fi

PANE=$(tmux display-message -p -t "$TARGET" '#{{pane_id}}' 2>/dev/null)
PANE_PID=$(tmux display-message -p -t "$TARGET" '#{{pane_pid}}' 2>/dev/null)
PANE_START_COMMAND=$(tmux display-message -p -t "$TARGET" '#{{pane_start_command}}' 2>/dev/null)
PANE_CURRENT_COMMAND=$(tmux display-message -p -t "$TARGET" '#{{pane_current_command}}' 2>/dev/null)
SESSION_ID=$(tmux display-message -p -t "$TARGET" '#{{session_id}}' 2>/dev/null)
WINDOW_ID=$(tmux display-message -p -t "$TARGET" '#{{window_id}}' 2>/dev/null)
CURRENT_IDENTITY="$PANE|$PANE_PID|$PANE_START_COMMAND|$PANE_CURRENT_COMMAND|$SESSION_ID|$WINDOW_ID"
if [ "$CURRENT_IDENTITY" != "$EXPECTED_IDENTITY" ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 失敗: 予約時と異なるtmux paneです: $TARGET" >> "$LOG"
  exit 1
fi

BEFORE=$(tmux capture-pane -p -t "$PANE")
BEFORE_CURSOR_Y=$(tmux display-message -p -t "$PANE" '#{{cursor_y}}' 2>/dev/null)
sleep 2
AFTER=$(tmux capture-pane -p -t "$PANE")
AFTER_CURSOR_Y=$(tmux display-message -p -t "$PANE" '#{{cursor_y}}' 2>/dev/null)
AFTER_CURRENT_COMMAND=$(tmux display-message -p -t "$PANE" '#{{pane_current_command}}' 2>/dev/null)
case "$AFTER_CURSOR_Y" in
  ''|*[!0-9]*) CURSOR_LINE='' ;;
  *) CURSOR_LINE=$(printf '%s\\n' "$AFTER" | awk -v row="$((AFTER_CURSOR_Y + 1))" 'NR == row {{ print; exit }}') ;;
esac
if [ "$BEFORE" != "$AFTER" ] || [ "$BEFORE_CURSOR_Y" != "$AFTER_CURSOR_Y" ] || [ "$PANE_CURRENT_COMMAND" != "$AFTER_CURRENT_COMMAND" ] || ! printf '%s\\n' "$CURSOR_LINE" | LC_ALL=C tr '\\302\\240' '  ' | LC_ALL=C grep -Eq '^(❯|›)[[:space:]]*$'; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 中断: $PANE は生成中、入力済み、または入力待ちを確認できません" >> "$LOG"
  printf '{{"name":"{job.name}","status":"blocked","mode":"existing_tmux","tmux_target":"%s","updated_at":"%s"}}' "$PANE" "$(date -Iseconds)" > "$STATE"
  exit 1
fi

tmux send-keys -t "$PANE" C-u &&
tmux load-buffer -b "$BUFFER" "$PROMPT" &&
tmux paste-buffer -d -b "$BUFFER" -t "$PANE" &&
sleep 1 &&
tmux send-keys -t "$PANE" Enter
rc=$?

if [ $rc -eq 0 ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 投入成功: $PANE" >> "$LOG"
  printf '{{"name":"{job.name}","status":"sent","mode":"existing_tmux","tmux_target":"%s","sent_at":"%s"}}' "$PANE" "$(date -Iseconds)" > "$STATE"
else
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 失敗: tmux投入が rc=$rc で終了" >> "$LOG"
  printf '{{"name":"{job.name}","status":"failed","mode":"existing_tmux","tmux_target":"%s","updated_at":"%s"}}' "$PANE" "$(date -Iseconds)" > "$STATE"
fi

exit $rc
"""
    runner.write_text(script, encoding="utf-8")
    runner.chmod(0o755)
    return runner


def write_runner(
    job: Job,
    tmux_target: str | None = None,
    tmux_identity: dict[str, str] | None = None,
) -> Path:
    """ジョブを実行する shell スクリプトを書き出す。

    launchd から呼ばれるので PATH が最小限。ログインシェル(-l)で起動して
    ~/.local/bin などにパスを通す必要がある。
    """
    target = tmux_target or job.tmux_target
    if target:
        identity = tmux_identity or tmux_target_identity(target)
        if identity is None:
            raise RuntimeError(f"tmux target {target!r} の同一性を確認できません")
        return _write_existing_tmux_runner(job, identity)

    runner = STATE_DIR / f"{job.name}.runner.sh"
    session = session_for(job.name)
    log_file = LOG_DIR / f"{job.name}.log"

    # プロンプトや引数はシェルに解釈させたくないので、Python 側で安全に引用する。

    claude_cmd = " ".join(
        shlex.quote(argument) for argument in job.claude_argv(include_prompt=False)
    )
    prompt_snapshot = prompt_path_for(job.name)
    _write_private_text(prompt_snapshot, job.prepared_prompt())
    quoted_cwd = shlex.quote(str(job.cwd))
    quoted_session = shlex.quote(session)
    quoted_log = shlex.quote(str(log_file))
    quoted_state = shlex.quote(str(state_path_for(job.name)))
    quoted_prompt = shlex.quote(str(prompt_snapshot))

    script = f"""#!/bin/zsh -l
# AutoPrompter runner (自動生成 - 直接編集しないこと)
# ジョブ: {job.name}
set -u

LOG={quoted_log}
SESSION={quoted_session}
STATE={quoted_state}
PROMPT={quoted_prompt}

cleanup_prompt() {{
  rm -f "$PROMPT"
}}
trap cleanup_prompt EXIT

echo "[$(date '+%Y-%m-%d %H:%M:%S')] ジョブ開始: {job.name}" >> "$LOG"

if ! command -v tmux >/dev/null 2>&1; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 失敗: tmux が見つかりません" >> "$LOG"
  exit 1
fi
if ! command -v claude >/dev/null 2>&1; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 失敗: claude が見つかりません" >> "$LOG"
  exit 1
fi

# 同名セッションが残っていると起動できない。既存があれば中断してまさとの判断に委ねる。
if tmux has-session -t "=$SESSION" 2>/dev/null; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 中断: セッション $SESSION が既に存在します" >> "$LOG"
  exit 1
fi

# -d でデタッチ起動。プロンプトは claude の位置引数として渡すので、
# キー入力の模擬(send-keys)は一切不要。
tmux new-session -d -s "$SESSION" -x 200 -y 50 -c {quoted_cwd} \\
  {claude_cmd} "$(cat "$PROMPT")"
rc=$?

if [ $rc -eq 0 ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 起動成功: tmux attach -t $SESSION で合流できます" >> "$LOG"
  printf '%s' "{{\\"name\\": \\"{job.name}\\", \\"status\\": \\"launched\\", \\"session\\": \\"$SESSION\\", \\"launched_at\\": \\"$(date -Iseconds)\\"}}" > "$STATE"
else
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 失敗: tmux new-session が rc=$rc で終了" >> "$LOG"
  printf '%s' "{{\\"name\\": \\"{job.name}\\", \\"status\\": \\"failed\\", \\"launched_at\\": \\"$(date -Iseconds)\\"}}" > "$STATE"
fi

exit $rc
"""
    runner.write_text(script, encoding="utf-8")
    runner.chmod(0o755)
    return runner


# ---------------------------------------------------------------------------
# launchd
# ---------------------------------------------------------------------------


def write_plist(job: Job, runner: Path, when: datetime) -> Path:
    """指定時刻に1回だけ発火する LaunchAgent を書き出す。"""
    label = label_for(job.name)
    path = plist_path_for(job.name)

    # StartCalendarInterval に Month/Day まで含めると、その日時に1回だけ発火する。
    # (発火後は cancel で撤去する運用)
    plist = {
        "Label": label,
        "ProgramArguments": [str(runner)],
        "StartCalendarInterval": {
            "Month": when.month,
            "Day": when.day,
            "Hour": when.hour,
            "Minute": when.minute,
        },
        "StandardOutPath": str(LOG_DIR / f"{job.name}.launchd.out.log"),
        "StandardErrorPath": str(LOG_DIR / f"{job.name}.launchd.err.log"),
        "RunAtLoad": False,
    }

    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        plistlib.dump(plist, f)
    return path


def launchctl_bootstrap(path: Path) -> None:
    label = path.stem
    # 同名が残っていると bootstrap が失敗するので、先に確実に外す。
    subprocess.run(
        ["launchctl", "bootout", f"{gui_domain()}/{label}"],
        capture_output=True,
    )
    r = subprocess.run(
        ["launchctl", "bootstrap", gui_domain(), str(path)],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        die(f"launchctl bootstrap に失敗しました:\n{r.stderr.strip()}")


def launchctl_bootout(name: str) -> bool:
    r = subprocess.run(
        ["launchctl", "bootout", f"{gui_domain()}/{label_for(name)}"],
        capture_output=True,
    )
    return r.returncode == 0


def scheduled_jobs() -> dict[str, dict]:
    """登録済みの AutoPrompter ジョブを plist から拾う。"""
    out: dict[str, dict] = {}
    if not LAUNCH_AGENTS_DIR.is_dir():
        return out
    for p in LAUNCH_AGENTS_DIR.glob(f"{LABEL_PREFIX}.*.plist"):
        name = p.stem[len(LABEL_PREFIX) + 1 :]
        try:
            with p.open("rb") as f:
                data = plistlib.load(f)
        except Exception:
            continue
        out[name] = {"plist": p, "data": data}
    return out


# ---------------------------------------------------------------------------
# watchdog (レートリミット自動対応)
# ---------------------------------------------------------------------------
#
# 動作の流れ (on_rate_limit != "none" のジョブのみ対象):
#   1. 5時間枠の使用率が rate_limit_threshold(既定95%)を超えている
#   2. かつ、そのジョブの tmux セッションが本当に止まっている(idle)
#      -> レートリミットで止まったと推定して「試し打ち」を送る
#   3. 試し打ちに応答があれば、リミットは実は回復している。何もしない
#      (会話は続いているので、まさとが後で attach すればよい)
#   4. 試し打ちに応答がなければ、on_rate_limit の設定に従う:
#        "continue" -> ログに記録するだけ(まさとの手動 continue を待つ)
#        "codex"    -> Codex に引き継ぐ
#
# ダイアログ等レートリミット以外の理由で止まっている場合は判別できない
# ("unknown" 状態は無視して何もしない)。これは意図的な安全側の判断。


def load_watchdog_state(name: str) -> dict:
    p = watchdog_state_path_for(name)
    if not p.exists():
        return {"phase": "watching"}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"phase": "watching"}


def save_watchdog_state(name: str, state: dict) -> None:
    watchdog_state_path_for(name).write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def watchdog_log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    with (LOG_DIR / "watchdog.log").open("a", encoding="utf-8") as f:
        f.write(line)


_PROBE_PROMPT = "watchdog: これが読めていたら「OK」とだけ返してください。"


def watchdog_probe_and_react(name: str, job_dict: dict) -> None:
    """1ジョブぶんの試し打ち〜対応を行う。job_dict は state/<name>.json の中身。"""
    session = session_for(name)
    if not tmux_session_exists(session):
        return  # セッションが無ければ watchdog の対象外

    on_rate_limit = job_dict.get("on_rate_limit", "none")
    if on_rate_limit == "none":
        return

    wd_state = load_watchdog_state(name)
    if wd_state.get("phase") in ("tried_continue", "handed_to_codex", "handoff_failed"):
        return  # 既に一度対応済み。二度手間・二重投入を避ける

    # 本当に止まっているか(名残スピナーに惑わされないか)を確認してから動く。
    if not tmux_session_is_idle(session, samples=3, interval=2.0):
        return

    watchdog_log(f"{name}: 閾値超過かつアイドル検知。試し打ちを送ります")
    tmux_send_prompt(session, _PROBE_PROMPT)

    # 試し打ちへの応答を少し待つ。応答が始まればすぐ画面が動くはず。
    responded = False
    for _ in range(6):
        time.sleep(5)
        if tmux_session_state(session) in ("generating", "idle"):
            screen = tmux_capture(session)
            if "OK" in screen.split(_PROBE_PROMPT)[-1]:
                responded = True
                break

    if responded:
        watchdog_log(f"{name}: 試し打ちに応答あり。リミットは回復している様子。継続")
        save_watchdog_state(name, {"phase": "watching"})
        return

    watchdog_log(f"{name}: 試し打ちに応答なし。on_rate_limit={on_rate_limit!r} に従います")

    if on_rate_limit == "continue":
        save_watchdog_state(name, {"phase": "tried_continue", "at": datetime.now().isoformat()})
        watchdog_log(f"{name}: continue 設定のため記録のみ。まさとの手動対応を待ちます")
        return

    if on_rate_limit == "codex":
        succeeded = handoff_to_codex(name, session)
        phase = "handed_to_codex" if succeeded else "handoff_failed"
        save_watchdog_state(name, {"phase": phase, "at": datetime.now().isoformat()})


def _write_codex_handoff_runner(name: str, prompt: str) -> Path:
    prompt_path = STATE_DIR / f"{name}.handoff.prompt.txt"
    runner_path = STATE_DIR / f"{name}.handoff.runner.sh"
    _write_private_text(prompt_path, prompt)
    quoted_prompt_path = shlex.quote(str(prompt_path))
    script = f"""#!/bin/zsh -l
set -u
PROMPT={quoted_prompt_path}
cleanup_prompt() {{
  rm -f "$PROMPT"
}}
trap cleanup_prompt EXIT
HANDOFF_PROMPT=$(cat "$PROMPT")
rm -f "$PROMPT"
trap - EXIT
rm -f "$0"
exec codex "$HANDOFF_PROMPT"
"""
    _write_private_text(runner_path, script)
    runner_path.chmod(0o700)
    return runner_path


def _cleanup_codex_handoff_files(name: str, runner: Path | None = None) -> None:
    (STATE_DIR / f"{name}.handoff.prompt.txt").unlink(missing_ok=True)
    (runner or STATE_DIR / f"{name}.handoff.runner.sh").unlink(missing_ok=True)


def _is_codex_command(command: str | None) -> bool:
    return bool(command and Path(command).name.lower().startswith("codex"))


def handoff_to_codex(name: str, session: str) -> bool:
    """tmux セッション内の claude を終え、直近の状況を要約したプロンプトで
    同じセッション内に codex を起動する。会話の器(tmux セッション)は
    維持したまま、中身のツールだけ切り替えるイメージ。
    """
    screen = tmux_capture(session)
    tail = "\n".join(screen.splitlines()[-60:])  # 直近の文脈のみ渡す

    handoff_prompt = (
        "これは Claude Code がレートリミットで停止したため、Codex に引き継がれた"
        "作業です。以下は直前の画面の抜粋です。状況を把握したうえで、"
        "妥当と思われる続きを進めてください。\n\n"
        "--- 直前の画面(抜粋) ---\n"
        f"{tail}\n"
        "--- ここまで ---\n"
    )

    watchdog_log(f"{name}: Claudeを終了し、Codexへの引き継ぎを開始します")
    tmux_send_prompt(session, "/exit")

    shell_commands = {"zsh", "bash", "fish", "sh", "dash"}
    for _ in range(10):
        time.sleep(1)
        if tmux_pane_current_command(session) in shell_commands:
            break
    else:
        watchdog_log(f"{name}: Claudeの終了を確認できないため、Codex起動を中断しました")
        return False

    try:
        runner = _write_codex_handoff_runner(name, handoff_prompt)
    except OSError as error:
        _cleanup_codex_handoff_files(name)
        watchdog_log(f"{name}: Codex runnerの作成に失敗しました: {error}")
        return False
    command = f"exec {shlex.quote(str(runner))}"
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", session, "-l", command],
            check=True,
        )
        subprocess.run(["tmux", "send-keys", "-t", session, "Enter"], check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        _cleanup_codex_handoff_files(name, runner)
        watchdog_log(f"{name}: Codex runnerの投入に失敗しました: {error}")
        return False

    for _ in range(10):
        time.sleep(1)
        if _is_codex_command(tmux_pane_current_command(session)):
            watchdog_log(f"{name}: Codexの起動を確認しました")
            return True

    _cleanup_codex_handoff_files(name, runner)
    watchdog_log(f"{name}: Codexの起動を確認できなかったため引き継ぎ失敗とします")
    return False


def run_watchdog_cycle() -> None:
    """全ジョブを1周チェックする。launchd から定期的に呼ばれる想定。"""
    ensure_dirs()
    result = five_hour_utilization()
    if result is None:
        watchdog_log("使用率を取得できませんでした。~/.claude.json 未整備の可能性")
        return
    utilization, resets_at = result

    for state_file in STATE_DIR.glob("*.json"):
        if state_file.name.endswith(".watchdog.json"):
            continue
        try:
            job_dict = json.loads(state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        name = job_dict.get("name")
        if not name:
            continue
        threshold = job_dict.get("rate_limit_threshold", DEFAULT_RATE_LIMIT_THRESHOLD)
        if utilization < threshold:
            continue
        watchdog_probe_and_react(name, job_dict)


# ---------------------------------------------------------------------------
# サブコマンド
# ---------------------------------------------------------------------------


def warn_job_caveats(job: Job) -> None:
    """ジョブ登録・実行の前に、知っておくべき挙動を知らせる。"""

    if job.tmux_target and any(
        value is not None for value in (job.model, job.effort, job.permission_mode)
    ):
        warn(
            "tmux_targetを使うジョブではmodel/effort/permission_modeは使われません。"
            "既存セッションの起動時設定がそのまま使われます。"
        )

    # 見ていない間に無確認でファイル変更・コマンド実行を許す設定。止めはしない。
    if job.permission_mode in RISKY_PERMISSION_MODES:
        warn(
            f"permission_mode={job.permission_mode!r} は、まさとが見ていない間に "
            "Claude がファイル変更やコマンド実行を無確認で行うことを意味します。"
        )

    # プランモードは計画立案のために CLI が内部で上位モデルへ差し替える(実測確認済み)。
    # --model の指定は無視されるので、指定した意味がないことを伝える。
    if job.permission_mode == "plan" and job.model:
        warn(
            f"permission_mode='plan' のとき、model={job.model!r} の指定は効きません。\n"
            "  プランモードは計画立案のため Claude Code が内部で上位モデルに切り替えます。\n"
            "  指定したモデルで走らせたい場合は permission_mode を外してください。"
        )


def cmd_add(args: argparse.Namespace) -> None:
    ensure_dirs()
    job = load_job(Path(args.job_file).resolve())
    when = parse_at(args.at)

    if when <= datetime.now():
        die(f"指定時刻が過去です: {when:%Y-%m-%d %H:%M}")

    warn_job_caveats(job)

    resolved_target = None
    session = session_for(job.name)
    if job.tmux_target:
        resolved_target = tmux_resolve_target(job.tmux_target)
        if resolved_target is None:
            die(f"tmux target {job.tmux_target!r} が見つかりません")
    else:
        # 未信頼ディレクトリだと起動時の確認ダイアログで止まり、無人実行が空振りする。
        trusted = is_trusted_dir(job.cwd)  # type: ignore[arg-type]
        if trusted is False:
            warn(
                f"{job.cwd} は Claude Code の信頼済みディレクトリではないようです。\n"
                "  そのままだと起動時の確認ダイアログで止まり、ジョブが空振りします。\n"
                f"  先に一度 `cd {job.cwd} && claude` を手動実行して承認しておいてください。"
            )
        elif trusted is None:
            warn("信頼済みディレクトリかどうか判定できませんでした（起動時に確認が出る可能性があります）")

        if tmux_session_exists(session):
            die(
                f"tmux セッション {session} が既に存在します。\n"
                f"  先に `autoprompt kill {job.name}` で片付けるか、別の name にしてください。"
            )

    runner = write_runner(job, resolved_target)
    plist = write_plist(job, runner, when)
    launchctl_bootstrap(plist)

    state_path_for(job.name).write_text(
        json.dumps(
            {
                "name": job.name,
                "status": "scheduled",
                "scheduled_for": when.isoformat(),
                "job_file": str(job.source),
                "mode": "existing_tmux" if resolved_target else "new_session",
                "cwd": str(job.cwd) if job.cwd else None,
                "model": job.model,
                "effort": job.effort,
                "permission_mode": job.permission_mode,
                "session": session if not resolved_target else None,
                "tmux_target": resolved_target,
                "on_rate_limit": job.on_rate_limit,
                "rate_limit_threshold": job.rate_limit_threshold,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"予約しました: {job.name}")
    print(f"  実行時刻   : {when:%Y-%m-%d %H:%M}")
    if resolved_target:
        print(f"  投入先     : {job.tmux_target} -> {resolved_target}")
        print(f"  内容       : {len(job.prepared_prompt())}文字（本文は表示しません）")
    else:
        print(f"  作業ディレクトリ: {job.cwd}")
        print(f"  モデル/effort  : {job.model or '(既定)'} / {job.effort or '(既定)'}")
        print(f"  権限モード : {job.permission_mode or '(既定: 毎回確認)'}")
        print(f"  合流方法   : autoprompt attach {job.name}")

    # on_rate_limit を設定したのに watchdog 本体が動いていないと、
    # 閾値を超えても誰も見ておらず引き継ぎが発火しない。取りこぼし防止で自動起動する。
    if not resolved_target and job.on_rate_limit != "none" and ensure_watchdog_running():
        print(f"  watchdog   : 未起動だったため自動起動しました({WATCHDOG_INTERVAL_MINUTES}分おき巡回)")


def cmd_list(args: argparse.Namespace) -> None:
    ensure_dirs()
    jobs = scheduled_jobs()

    # tmux 側に生きているセッションも拾う(予約は消えたが会話が残っている場合)。
    r = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True,
        text=True,
    )
    live = {
        s[len(TMUX_PREFIX) :]
        for s in r.stdout.split()
        if s.startswith(TMUX_PREFIX)
    }

    names = sorted(set(jobs) | live)
    if not names:
        print("予約も実行中セッションもありません。")
        return

    print(f"{'ジョブ名':<20} {'予約時刻':<18} {'状態':<12} 合流")
    print("-" * 72)
    for name in names:
        when_str = "-"
        if name in jobs:
            sci = jobs[name]["data"].get("StartCalendarInterval", {})
            if sci:
                when_str = (
                    f"{sci.get('Month', 0):02d}/{sci.get('Day', 0):02d} "
                    f"{sci.get('Hour', 0):02d}:{sci.get('Minute', 0):02d}"
                )

        if name in live:
            status = "実行中/待機"
            how = f"autoprompt attach {name}"
        elif name in jobs:
            status = "予約済み"
            how = "-"
        else:
            status = "?"
            how = "-"

        print(f"{name:<20} {when_str:<18} {status:<12} {how}")


def cmd_attach(args: argparse.Namespace) -> None:
    session = session_for(args.name)
    if not tmux_session_exists(session):
        die(
            f"セッション {session} は存在しません。\n"
            "  まだ実行されていないか、既に終了しています。"
            f"  ログ: {LOG_DIR / (args.name + '.log')}"
        )
    # exec で置き換えて、tmux に端末を明け渡す。
    os.execvp("tmux", ["tmux", "attach", "-t", f"={session}"])


def cmd_continue(args: argparse.Namespace) -> None:
    """レートリミットで止まったセッションに「続けて」を投げる。

    tmux セッションが生きている前提。応答待ちで止まっているだけなら
    そのまま送れるが、`claude` プロセス自体が終了している場合は
    入力欄が無いのでプロンプトが素通りする。事前に画面を見て
    判断したい場合は `autoprompt attach` で目視するほうが確実。
    """
    session = session_for(args.name)
    if not tmux_session_exists(session):
        die(
            f"セッション {session} は存在しません。\n"
            "  tmux セッションが残っていない場合は再開できません。"
            "新しくジョブを add/run してください。"
        )

    prompt = args.prompt or "続けて"
    tmux_send_prompt(session, prompt)

    print(f"投げました: {session} <- {prompt!r}")
    print(f"  確認: autoprompt attach {args.name}")


def _prompt_from_args(args: argparse.Namespace) -> str:
    if args.prompt and args.prompt_file:
        die("promptと--prompt-fileは同時に指定できません")
    if args.prompt_file:
        prompt_path = Path(os.path.expanduser(args.prompt_file)).resolve()
        if not prompt_path.is_file():
            die(f"prompt_fileが見つかりません: {prompt_path}")
        prompt = prompt_path.read_text(encoding="utf-8")
    else:
        prompt = args.prompt
    if not prompt or not prompt.strip():
        die("中身のあるpromptまたは--prompt-fileを指定してください")
    return prompt.strip()


def cmd_send(args: argparse.Namespace) -> None:
    """AutoPrompter管理外を含む既存tmux paneへ直接送信する。"""
    prompt = _prompt_from_args(args)
    pane_id = tmux_resolve_target(args.target)
    if pane_id is None:
        die(f"tmux target {args.target!r} が見つかりません")
    if not args.force:
        initial_command = tmux_pane_current_command(pane_id)
        if _is_shell_command(initial_command):
            die(f"{pane_id}はshell待機中のため、プロンプトをコマンドとして送信しません")
        if not tmux_target_is_idle(pane_id):
            die(
                f"{pane_id}が空の入力待ちであることを確認できません。"
                "生成中や入力途中の可能性があります。確認後、必要なら--forceを指定してください"
            )
        final_screen = tmux_capture(pane_id)
        final_cursor_y = tmux_pane_cursor_y(pane_id)
        final_command = tmux_pane_current_command(pane_id)
        if (
            final_command != initial_command
            or final_cursor_y is None
            or not _has_empty_agent_input_at_cursor(final_screen, final_cursor_y)
        ):
            die(f"{pane_id}の状態が確認中に変化したため、送信を中止しました")
    tmux_send_prompt(pane_id, prompt)
    print(f"投入しました: {args.target} -> {pane_id}（{len(prompt)}文字）")


def cmd_cancel(args: argparse.Namespace) -> None:
    ensure_dirs()
    name = args.name
    plist = plist_path_for(name)

    if not plist.exists():
        die(f"{name} の予約が見つかりません")

    launchctl_bootout(name)
    plist.unlink()

    runner = STATE_DIR / f"{name}.runner.sh"
    runner.unlink(missing_ok=True)
    prompt_path_for(name).unlink(missing_ok=True)
    state_path_for(name).unlink(missing_ok=True)

    print(f"予約を取り消しました: {name}")
    if tmux_session_exists(session_for(name)):
        print(
            f"  なお tmux セッション {session_for(name)} は生きています。"
            f"消すなら: autoprompt kill {name}"
        )


def cmd_kill(args: argparse.Namespace) -> None:
    session = session_for(args.name)
    if not tmux_session_exists(session):
        die(f"セッション {session} は存在しません")
    subprocess.run(["tmux", "kill-session", "-t", f"={session}"], check=False)
    print(f"セッションを終了しました: {session}")


def cmd_run(args: argparse.Namespace) -> None:
    """予約せず即座に実行する(動作確認用)。"""
    ensure_dirs()
    job = load_job(Path(args.job_file).resolve())

    warn_job_caveats(job)

    resolved_target = None
    if job.tmux_target:
        resolved_target = tmux_resolve_target(job.tmux_target)
        if resolved_target is None:
            die(f"tmux target {job.tmux_target!r} が見つかりません")
    else:
        session = session_for(job.name)
        if tmux_session_exists(session):
            die(f"tmux セッション {session} が既に存在します")

    runner = write_runner(job, resolved_target)
    r = subprocess.run([str(runner)], capture_output=True, text=True)
    if r.returncode != 0:
        die(f"実行に失敗しました (rc={r.returncode})\n{r.stdout}\n{r.stderr}")

    if resolved_target:
        print(f"投入しました: {job.tmux_target} -> {resolved_target}")
    else:
        print(f"起動しました: {job.name}")
        print(f"  合流方法: autoprompt attach {job.name}")


def watchdog_plist_path() -> Path:
    return LAUNCH_AGENTS_DIR / f"{WATCHDOG_LABEL}.plist"


def watchdog_is_running() -> bool:
    r = subprocess.run(
        ["launchctl", "print", f"{gui_domain()}/{WATCHDOG_LABEL}"],
        capture_output=True,
        text=True,
    )
    return r.returncode == 0


def start_watchdog() -> None:
    """定期監視(watchdog)を launchd に登録して起動する(内部処理本体)。"""
    ensure_dirs()
    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)

    # launchd から直接 python を起動すると PATH が最小限のままで、
    # tmux/claude を command -v で見つけられずに毎回失敗する(実測: exit 1)。
    # runner.sh と同様、ログインシェル(-l)経由にして PATH を解決させる。
    python_cmd = " ".join(shlex.quote(a) for a in (sys.executable, str(Path(__file__).resolve()), "watchdog-cycle"))
    plist = {
        "Label": WATCHDOG_LABEL,
        "ProgramArguments": ["/bin/zsh", "-l", "-c", python_cmd],
        "StartInterval": WATCHDOG_INTERVAL_MINUTES * 60,
        "RunAtLoad": True,
        "StandardOutPath": str(LOG_DIR / "watchdog.launchd.out.log"),
        "StandardErrorPath": str(LOG_DIR / "watchdog.launchd.err.log"),
    }
    path = watchdog_plist_path()
    with path.open("wb") as f:
        plistlib.dump(plist, f)

    subprocess.run(["launchctl", "bootout", f"{gui_domain()}/{WATCHDOG_LABEL}"], capture_output=True)
    r = subprocess.run(
        ["launchctl", "bootstrap", gui_domain(), str(path)], capture_output=True, text=True
    )
    if r.returncode != 0:
        die(f"launchctl bootstrap に失敗しました:\n{r.stderr.strip()}")


def ensure_watchdog_running() -> bool:
    """watchdog が未起動なら起動する。実際に起動した場合は True を返す。

    on_rate_limit を設定したジョブを add したのに watchdog 本体が
    起動しておらず引き継ぎが発火しない、という取りこぼしを防ぐための
    自動起動。既に稼働中なら何もしない(二重登録は launchctl 側で
    bootout してから bootstrap するので安全だが、無駄なログ出力を避ける)。
    """
    if watchdog_is_running():
        return False
    start_watchdog()
    return True


def cmd_watchdog_start(args: argparse.Namespace) -> None:
    start_watchdog()
    print(f"watchdog を起動しました({WATCHDOG_INTERVAL_MINUTES}分おきに巡回)")
    print("  on_rate_limit を設定したジョブのみが対象です。")
    print(f"  ログ: {LOG_DIR / 'watchdog.log'}")


def cmd_watchdog_stop(args: argparse.Namespace) -> None:
    r = subprocess.run(
        ["launchctl", "bootout", f"{gui_domain()}/{WATCHDOG_LABEL}"], capture_output=True
    )
    watchdog_plist_path().unlink(missing_ok=True)
    if r.returncode == 0:
        print("watchdog を停止しました")
    else:
        print("watchdog は登録されていませんでした")


def cmd_watchdog_status(args: argparse.Namespace) -> None:
    if not watchdog_is_running():
        print("watchdog は停止しています(未登録)")
        return
    r = subprocess.run(
        ["launchctl", "print", f"{gui_domain()}/{WATCHDOG_LABEL}"],
        capture_output=True,
        text=True,
    )
    print("watchdog は稼働中です")
    for line in r.stdout.splitlines():
        if any(k in line for k in ("state =", "runs =", "last exit")):
            print(f"  {line.strip()}")

    result = five_hour_utilization()
    if result:
        utilization, resets_at = result
        local_reset = resets_at.astimezone()
        print(f"  現在の5時間枠使用率: {utilization:.0f}% (回復: {local_reset:%H:%M})")


def cmd_watchdog_cycle(args: argparse.Namespace) -> None:
    """launchd から定期的に呼ばれる本体。人手では基本使わない。"""
    run_watchdog_cycle()


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="autoprompt",
        description="指定時刻に Claude Code セッションを起動してプロンプトを自動投入する",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="ジョブを指定時刻に予約する")
    p_add.add_argument("job_file", help="ジョブ定義の TOML ファイル")
    p_add.add_argument(
        "--at",
        required=True,
        help='実行時刻。"03:00" / "2026-08-17 03:00" / "+90m" / "+2h"',
    )
    p_add.set_defaults(func=cmd_add)

    p_list = sub.add_parser("list", help="予約と実行中セッションの一覧")
    p_list.set_defaults(func=cmd_list)

    p_attach = sub.add_parser("attach", help="セッションに合流して会話を続ける")
    p_attach.add_argument("name", help="ジョブ名")
    p_attach.set_defaults(func=cmd_attach)

    p_continue = sub.add_parser(
        "continue", help="止まっているセッションに続行プロンプトを投げる"
    )
    p_continue.add_argument("name", help="ジョブ名")
    p_continue.add_argument(
        "prompt",
        nargs="?",
        default=None,
        help='投げるプロンプト。省略すると "続けて"',
    )
    p_continue.set_defaults(func=cmd_continue)

    p_send = sub.add_parser(
        "send", help="既存のtmux session/window/paneへプロンプトを投入する"
    )
    p_send.add_argument("target", help="tmux target。例: hddp:0.0")
    p_send.add_argument("prompt", nargs="?", default=None, help="投入するプロンプト")
    p_send.add_argument(
        "--prompt-file",
        default=None,
        help="投入内容をUTF-8ファイルから読む（長文・機密情報向け）",
    )
    p_send.add_argument(
        "--force",
        action="store_true",
        help="生成中・入力途中の可能性があっても入力欄を置換して送る",
    )
    p_send.set_defaults(func=cmd_send)

    p_cancel = sub.add_parser("cancel", help="予約を取り消す")
    p_cancel.add_argument("name", help="ジョブ名")
    p_cancel.set_defaults(func=cmd_cancel)

    p_kill = sub.add_parser("kill", help="tmux セッションを終了する")
    p_kill.add_argument("name", help="ジョブ名")
    p_kill.set_defaults(func=cmd_kill)

    p_run = sub.add_parser("run", help="予約せず即座に実行する(動作確認用)")
    p_run.add_argument("job_file", help="ジョブ定義の TOML ファイル")
    p_run.set_defaults(func=cmd_run)

    p_wd_start = sub.add_parser(
        "watchdog-start", help="レートリミット自動対応の定期監視を開始する"
    )
    p_wd_start.set_defaults(func=cmd_watchdog_start)

    p_wd_stop = sub.add_parser("watchdog-stop", help="定期監視を停止する")
    p_wd_stop.set_defaults(func=cmd_watchdog_stop)

    p_wd_status = sub.add_parser("watchdog-status", help="定期監視の状態を確認する")
    p_wd_status.set_defaults(func=cmd_watchdog_status)

    p_wd_cycle = sub.add_parser(
        "watchdog-cycle", help="監視を1回だけ実行する(launchdから呼ばれる内部用)"
    )
    p_wd_cycle.set_defaults(func=cmd_watchdog_cycle)

    args = parser.parse_args()

    if not shutil.which("tmux"):
        die("tmux が見つかりません。`brew install tmux` で導入してください")

    args.func(args)


if __name__ == "__main__":
    main()
