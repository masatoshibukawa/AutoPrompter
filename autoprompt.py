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
import shutil
import subprocess
import sys
import tomllib
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
        self.cwd: Path = data["cwd"]
        self.model: str | None = data.get("model")
        self.effort: str | None = data.get("effort")
        self.permission_mode: str | None = data.get("permission_mode")
        self.prompt: str = data["prompt"]
        self.auto_decide: bool = bool(data.get("auto_decide", False))

    def claude_argv(self) -> list[str]:
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
        prompt = self.prompt + AUTO_DECIDE_SUFFIX if self.auto_decide else self.prompt
        argv.append(prompt)
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

    # --- cwd ---
    raw_cwd = data.get("cwd")
    if not raw_cwd:
        die("cwd は必須です (Claude を起動する作業ディレクトリ)")
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

    return Job(
        {
            "name": name,
            "cwd": cwd,
            "model": model,
            "effort": effort,
            "permission_mode": permission_mode,
            "prompt": prompt,
            "auto_decide": data.get("auto_decide", False),
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


def tmux_send_prompt(session: str, prompt: str) -> None:
    """生きている tmux セッションの入力欄にプロンプトを投入して送信する。

    複数行を生の改行のまま送ると Claude Code は1行ごとに別メッセージとして
    送信してしまう(実測確認済み)。Claude Code の入力欄はソフト改行に
    "\\<改行>" を使う仕様なので、末尾以外の改行をそれに置き換えてから
    load-buffer + paste-buffer で流し込む。
    """
    import tempfile

    lines = prompt.split("\n")
    literal_prompt = "\\\n".join(lines)

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(literal_prompt)
        buf_path = f.name

    try:
        # 入力欄を確実に空にしてから貼り付ける。
        subprocess.run(["tmux", "send-keys", "-t", session, "C-u"], check=False)
        subprocess.run(
            ["tmux", "load-buffer", "-b", "autoprompt", buf_path], check=True
        )
        subprocess.run(
            ["tmux", "paste-buffer", "-d", "-b", "autoprompt", "-t", session],
            check=True,
        )
        # 貼り付けの描画待ち。実測で必要だった待ち時間。
        subprocess.run(["sleep", "1"])
        subprocess.run(["tmux", "send-keys", "-t", session, "Enter"], check=False)
    finally:
        Path(buf_path).unlink(missing_ok=True)


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
# runner スクリプトの生成
# ---------------------------------------------------------------------------


def write_runner(job: Job) -> Path:
    """ジョブを実行する shell スクリプトを書き出す。

    launchd から呼ばれるので PATH が最小限。ログインシェル(-l)で起動して
    ~/.local/bin などにパスを通す必要がある。
    """
    runner = STATE_DIR / f"{job.name}.runner.sh"
    session = session_for(job.name)
    log_file = LOG_DIR / f"{job.name}.log"

    # プロンプトや引数はシェルに解釈させたくないので、Python 側で安全に引用する。
    import shlex

    claude_cmd = " ".join(shlex.quote(a) for a in job.claude_argv())
    quoted_cwd = shlex.quote(str(job.cwd))
    quoted_session = shlex.quote(session)
    quoted_log = shlex.quote(str(log_file))
    quoted_state = shlex.quote(str(state_path_for(job.name)))

    script = f"""#!/bin/zsh -l
# AutoPrompter runner (自動生成 - 直接編集しないこと)
# ジョブ: {job.name}
set -u

LOG={quoted_log}
SESSION={quoted_session}
STATE={quoted_state}

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
  {claude_cmd}
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
# サブコマンド
# ---------------------------------------------------------------------------


def warn_job_caveats(job: Job) -> None:
    """ジョブ登録・実行の前に、知っておくべき挙動を知らせる。"""

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

    # 未信頼ディレクトリだと起動時の確認ダイアログで止まり、無人実行が空振りする。
    trusted = is_trusted_dir(job.cwd)
    if trusted is False:
        warn(
            f"{job.cwd} は Claude Code の信頼済みディレクトリではないようです。\n"
            "  そのままだと起動時の確認ダイアログで止まり、ジョブが空振りします。\n"
            f"  先に一度 `cd {job.cwd} && claude` を手動実行して承認しておいてください。"
        )
    elif trusted is None:
        warn("信頼済みディレクトリかどうか判定できませんでした（起動時に確認が出る可能性があります）")

    session = session_for(job.name)
    if tmux_session_exists(session):
        die(
            f"tmux セッション {session} が既に存在します。\n"
            f"  先に `autoprompt kill {job.name}` で片付けるか、別の name にしてください。"
        )

    runner = write_runner(job)
    plist = write_plist(job, runner, when)
    launchctl_bootstrap(plist)

    state_path_for(job.name).write_text(
        json.dumps(
            {
                "name": job.name,
                "status": "scheduled",
                "scheduled_for": when.isoformat(),
                "job_file": str(job.source),
                "cwd": str(job.cwd),
                "model": job.model,
                "effort": job.effort,
                "permission_mode": job.permission_mode,
                "session": session,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"予約しました: {job.name}")
    print(f"  実行時刻   : {when:%Y-%m-%d %H:%M}")
    print(f"  作業ディレクトリ: {job.cwd}")
    print(f"  モデル/effort  : {job.model or '(既定)'} / {job.effort or '(既定)'}")
    print(f"  権限モード : {job.permission_mode or '(既定: 毎回確認)'}")
    print(f"  合流方法   : autoprompt attach {job.name}")


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

    session = session_for(job.name)
    if tmux_session_exists(session):
        die(f"tmux セッション {session} が既に存在します")

    runner = write_runner(job)
    r = subprocess.run([str(runner)], capture_output=True, text=True)
    if r.returncode != 0:
        die(f"実行に失敗しました (rc={r.returncode})\n{r.stdout}\n{r.stderr}")

    print(f"起動しました: {job.name}")
    print(f"  合流方法: autoprompt attach {job.name}")


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

    p_cancel = sub.add_parser("cancel", help="予約を取り消す")
    p_cancel.add_argument("name", help="ジョブ名")
    p_cancel.set_defaults(func=cmd_cancel)

    p_kill = sub.add_parser("kill", help="tmux セッションを終了する")
    p_kill.add_argument("name", help="ジョブ名")
    p_kill.set_defaults(func=cmd_kill)

    p_run = sub.add_parser("run", help="予約せず即座に実行する(動作確認用)")
    p_run.add_argument("job_file", help="ジョブ定義の TOML ファイル")
    p_run.set_defaults(func=cmd_run)

    args = parser.parse_args()

    if not shutil.which("tmux"):
        die("tmux が見つかりません。`brew install tmux` で導入してください")

    args.func(args)


if __name__ == "__main__":
    main()
