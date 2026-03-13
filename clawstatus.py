#!/usr/bin/env python3
"""
ClawStatus - OpenClaw 状态看板

核心能力：
1) Agent / Sub-agent 运行概览
2) Cron 任务数量与执行状态
3) OpenClaw 总体健康状态
4) 模型配置数量与各模型 token 处理量（基于 sessions 快照聚合）
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import signal
import subprocess
import threading
import sys
import time
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, jsonify, request, Response

try:
    from waitress import serve as waitress_serve
except Exception:  # pragma: no cover
    waitress_serve = None

__version__ = "2.0.0"
APP_TITLE = "ClawStatus"

HOME = Path.home()
OPENCLAW_DIR = HOME / ".openclaw"
OPENCLAW_CONFIG = OPENCLAW_DIR / "openclaw.json"
AGENTS_DIR = OPENCLAW_DIR / "agents"
CRON_JOBS_PATH = OPENCLAW_DIR / "cron" / "jobs.json"
CRON_RUNS_DIR = OPENCLAW_DIR / "cron" / "runs"
SUBAGENT_RUNS_PATH = OPENCLAW_DIR / "subagents" / "runs.json"

RUNTIME_DIR = HOME / ".clawstatus"
PID_FILE = RUNTIME_DIR / "clawstatus.pid"
LOG_FILE = RUNTIME_DIR / "clawstatus.log"

ACTIVE_AGENT_WINDOW_MS = 5 * 60 * 1000
_REFRESH_INTERVAL_SEC = 30
_STATUS_CACHE_TTL_SEC = 60
_STATUS_WARMUP_INTERVAL_SEC = 120
_STATUS_WARMUP_IDLE_GRACE_SEC = 600
_DASH_CACHE_TTL_SEC = 30
_DAILY_TOKENS_TTL_SEC = 300
_DAILY_FILELIST_TTL_SEC = 900
_MODELS_CACHE_TTL_SEC = 300
_MEMORY_CACHE_TTL_SEC = 300
_status_cache: Dict[str, Any] = {"ts": 0.0, "data": None, "err": None}
_dash_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
_daily_tokens_cache: Dict[str, Any] = {
    "ts": 0.0,
    "rows": None,
    "dayMap": None,
    "dayActiveMap": None,
    "dayPassiveMap": None,
    "fileState": {},
    "fileContrib": {},
    "fileContribActive": {},
    "fileContribPassive": {},
    "days": None,
    "startDay": None,
    "files": None,
    "filesTs": 0.0,
    "dirsStamp": None,
}
_models_cache: Dict[str, Any] = {"ts": 0.0, "data": None, "days": 0}
_memory_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
_status_lock = threading.Lock()
_dash_lock = threading.Lock()
_daily_tokens_lock = threading.Lock()
_models_lock = threading.Lock()
_memory_lock = threading.Lock()
_bg_warmup_started = False
_dash_refreshing = False
_status_refreshing = False
_last_status_demand_ts = 0.0


def _cache_ts_ms(cache: Dict[str, Any], lock: threading.Lock) -> Optional[int]:
    with lock:
        ts = float(cache.get("ts", 0) or 0)
    if ts <= 0:
        return None
    return int(ts * 1000)


def _safe_read_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _read_json_tolerant(path: Path, default: Any) -> Any:
    """读取有轻微格式错误的 JSON 文件（仅在解析断点处尝试补逗号）。"""
    try:
        if not path.exists():
            return default
        text = path.read_text(encoding="utf-8", errors="replace")
        return json.loads(text)
    except json.JSONDecodeError as e:
        # 仅在报错位置附近是“值与值直接相邻”时，尝试插入一个逗号
        i = int(e.pos) - 1
        j = int(e.pos)
        while i >= 0 and text[i].isspace():
            i -= 1
        while j < len(text) and text[j].isspace():
            j += 1

        if i >= 0 and j < len(text):
            left = text[i]
            right = text[j]
            right_starts = set('{["-0123456789tfn')
            if left in "]}" and right in right_starts:
                try:
                    fixed = text[:j] + "," + text[j:]
                    return json.loads(fixed)
                except Exception:
                    return default
        return default
    except Exception:
        return default


def _fmt_ts(ms: Optional[int]) -> str:
    if not ms:
        return "-"
    try:
        return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "-"


def _fmt_duration_ms(ms: Optional[int]) -> str:
    if ms is None:
        return "-"
    sec = int(ms / 1000)
    if sec < 60:
        return f"{sec}s"
    mins, s = divmod(sec, 60)
    if mins < 60:
        return f"{mins}m{s:02d}s"
    hrs, m = divmod(mins, 60)
    return f"{hrs}h{m:02d}m"


def _extract_json_blob(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None

    try:
        return json.loads(text)
    except Exception:
        pass

    decoder = json.JSONDecoder()
    idx = 0
    best: Optional[Dict[str, Any]] = None

    while True:
        start = text.find("{", idx)
        if start < 0:
            break
        try:
            obj, end = decoder.raw_decode(text, start)
            if isinstance(obj, dict):
                # 优先返回更像 openclaw status 的对象
                if any(k in obj for k in ("heartbeat", "gateway", "sessions", "agents", "channelSummary")):
                    return obj
                if best is None or len(obj) > len(best):
                    best = obj
            idx = max(end, start + 1)
        except Exception:
            idx = start + 1

    return best


def _run_status_cmd(cmd: List[str], timeout_sec: int) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except FileNotFoundError:
        return None, "openclaw 命令不存在（PATH 未找到）"
    except subprocess.TimeoutExpired:
        return None, f"{' '.join(cmd[1:])} 执行超时"
    except Exception as e:
        return None, f"openclaw status 调用失败: {e}"

    merged = (proc.stdout or "") + "\n" + (proc.stderr or "")
    data = _extract_json_blob(merged)
    if data is not None:
        return data, None

    snippet = merged.strip().splitlines()[:3]
    hint = " | ".join(snippet) if snippet else "无输出"
    return None, f"无法解析 openclaw status 输出（rc={proc.returncode}）：{hint[:240]}"


def _refresh_openclaw_status() -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """执行一次真实的 openclaw status 刷新，并写入缓存。"""
    global _status_refreshing

    now = time.time()
    openclaw_bin = os.environ.get("OPENCLAW_BIN") or shutil.which("openclaw") or str(HOME / ".npm-global" / "bin" / "openclaw")

    # 默认使用轻量路径，降低周期性预热对系统资源的影响。
    attempts = [([openclaw_bin, "status", "--json"], 20)]

    # 如需强制拉 usage，可通过环境变量启用；默认关闭。
    if (os.environ.get("CLAWSTATUS_ENABLE_STATUS_USAGE") or "").strip() == "1":
        attempts.append(([openclaw_bin, "status", "--json", "--usage"], 25))

    last_err: Optional[str] = None
    try:
        for cmd, timeout_sec in attempts:
            data, err = _run_status_cmd(cmd, timeout_sec)
            if data is not None:
                with _status_lock:
                    _status_cache.update({"ts": now, "data": data, "err": None})
                return data, None
            last_err = err

        with _status_lock:
            old = _status_cache.get("data")
            _status_cache.update({"ts": now, "data": old, "err": last_err})
        return old, last_err
    finally:
        with _status_lock:
            _status_refreshing = False


def _start_status_refresh() -> None:
    global _status_refreshing
    with _status_lock:
        if _status_refreshing:
            return
        _status_refreshing = True
    t = threading.Thread(target=_refresh_openclaw_status, daemon=True, name="clawstatus-status-refresh")
    t.start()


def _run_openclaw_status() -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """优先返回缓存，避免 API 请求阻塞。冷启动时才同步执行。"""
    global _last_status_demand_ts

    now = time.time()
    _last_status_demand_ts = now
    with _status_lock:
        cached_data = _status_cache.get("data")
        cached_err = _status_cache.get("err")
        cached_ts = float(_status_cache.get("ts", 0) or 0)

    # 缓存新鲜
    if cached_data is not None and now - cached_ts < _STATUS_CACHE_TTL_SEC:
        return cached_data, cached_err

    # 缓存过期但存在：直接返回旧值（速度优先），同时触发后台刷新（准确性优先）
    if cached_data is not None:
        _start_status_refresh()
        return cached_data, cached_err

    # 冷启动无缓存：同步拉一次，避免页面空白
    return _refresh_openclaw_status()


def _bg_status_warmup_loop() -> None:
    """后台静默预热状态缓存。"""
    _refresh_openclaw_status()  # 启动即预热
    while True:
        time.sleep(_STATUS_WARMUP_INTERVAL_SEC)

        # 长时间无人访问时暂停预热，避免无意义子进程开销
        idle_sec = time.time() - float(_last_status_demand_ts or 0)
        if idle_sec > _STATUS_WARMUP_IDLE_GRACE_SEC:
            continue

        _start_status_refresh()


def _load_auth_token() -> Optional[str]:
    env_token = (os.environ.get("OPENCLAW_GATEWAY_TOKEN") or "").strip()
    if env_token:
        return env_token

    env_token2 = (os.environ.get("CLAWSTATUS_TOKEN") or "").strip()
    if env_token2:
        return env_token2

    cfg = _safe_read_json(OPENCLAW_CONFIG, {})
    token = (
        cfg.get("gateway", {})
        .get("auth", {})
        .get("token", "")
        .strip()
    )
    return token or None


def _token_from_request() -> Optional[str]:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header.split(" ", 1)[1].strip()
    x_api_key = request.headers.get("X-API-Key", "").strip()
    if x_api_key:
        return x_api_key
    q_token = request.args.get("token", "").strip()
    return q_token or None


def _is_authorized(required_token: Optional[str]) -> bool:
    if not required_token:
        return True
    got = _token_from_request()
    return bool(got and got == required_token)


def _require_auth(required_token: Optional[str]):
    if _is_authorized(required_token):
        return None
    return jsonify({"error": "unauthorized", "valid": False}), 401


def _read_last_jsonl_obj(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists() or not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = [ln.strip() for ln in f.readlines() if ln.strip()]
        if not lines:
            return None
        return json.loads(lines[-1])
    except Exception:
        return None


def _collect_subagent_runs() -> Dict[str, Any]:
    raw = _safe_read_json(SUBAGENT_RUNS_PATH, {"runs": {}})
    runs_obj = raw.get("runs", {}) if isinstance(raw, dict) else {}
    runs: List[Dict[str, Any]] = []
    running_count = 0

    if isinstance(runs_obj, dict):
        items = runs_obj.items()
    elif isinstance(runs_obj, list):
        items = [(str(i), v) for i, v in enumerate(runs_obj)]
    else:
        items = []

    def _agent_from_session_key(sk: Any) -> Optional[str]:
        s = str(sk or "")
        if not s.startswith("agent:"):
            return None
        parts = s.split(":")
        if len(parts) < 2:
            return None
        return parts[1] or None

    for run_id, payload in items:
        if not isinstance(payload, dict):
            continue

        ended_at = payload.get("endedAt")
        explicit_status = str(
            payload.get("status")
            or payload.get("state")
            or payload.get("phase")
            or ""
        ).strip().lower()

        if explicit_status:
            status = explicit_status
        elif ended_at:
            outcome = payload.get("outcome") if isinstance(payload.get("outcome"), dict) else {}
            status = str(outcome.get("status") or payload.get("endedReason") or "completed").lower()
        elif payload.get("startedAt"):
            status = "running"
        elif payload.get("createdAt"):
            status = "queued"
        else:
            status = "unknown"

        is_running = (
            status in {"running", "active", "started", "in_progress", "processing"}
            or (payload.get("startedAt") is not None and payload.get("endedAt") is None)
        )
        if is_running:
            running_count += 1

        child_sk = payload.get("childSessionKey") or payload.get("sessionKey")
        requester_sk = payload.get("requesterSessionKey")

        agent_id = payload.get("agentId") or _agent_from_session_key(child_sk)
        parent_agent_id = payload.get("parentAgentId") or _agent_from_session_key(requester_sk)

        runs.append(
            {
                "id": payload.get("runId") or run_id,
                "status": status,
                "agentId": agent_id,
                "parentAgentId": parent_agent_id,
                "sessionKey": child_sk,
                "childSessionKey": child_sk,
                "requesterSessionKey": requester_sk,
                "startedAt": payload.get("startedAt") or payload.get("createdAt"),
                "updatedAt": payload.get("updatedAt") or payload.get("endedAt"),
                "endedAt": payload.get("endedAt"),
                "task": payload.get("task"),
                "raw": payload,
            }
        )

    return {
        "total": len(runs),
        "running": running_count,
        "runs": runs,
    }


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _actual_consumed_tokens(input_tokens: Any, output_tokens: Any, cache_read_tokens: Any) -> int:
    gross_input = max(0, _safe_int(input_tokens))
    cache_reused = max(0, _safe_int(cache_read_tokens))
    output = max(0, _safe_int(output_tokens))
    # 实际消耗 Token = (总输入 Token − 缓存复用 Token) + 输出 Token
    return max(0, gross_input - cache_reused) + output


def _is_passive_session(session_key: str, rec: Dict[str, Any]) -> bool:
    sk = str(session_key or "").lower()
    if any(flag in sk for flag in (":cron:", ":run:", ":heartbeat:", ":scheduler:")):
        return True

    origin = rec.get("origin") if isinstance(rec, dict) else {}
    if not isinstance(origin, dict):
        origin = {}

    provider = str(origin.get("provider") or "").strip().lower()
    if provider in {"heartbeat", "cron", "scheduler", "system", "auto"}:
        return True

    source = str(rec.get("source") or rec.get("trigger") or "").strip().lower()
    if source in {"heartbeat", "cron", "scheduler", "system", "auto"}:
        return True

    last_account_id = str(rec.get("lastAccountId") or "").strip().lower()
    if last_account_id in {"cron", "auto"} and not provider:
        return True

    return False


def _usage_day_and_tokens_from_line(line: str, day_keys: set[str]) -> Optional[Tuple[str, int]]:
    line = (line or "").strip()
    if not line:
        return None

    try:
        obj = json.loads(line)
    except Exception:
        return None

    if obj.get("type") != "message":
        return None

    msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
    if not usage:
        return None

    input_tokens = usage.get("input")
    output_tokens = usage.get("output")
    cache_read = usage.get("cacheRead")

    has_formula_fields = any(v is not None for v in (input_tokens, output_tokens, cache_read))
    if has_formula_fields:
        consumed = _actual_consumed_tokens(input_tokens, output_tokens, cache_read)
    else:
        total_tokens = usage.get("totalTokens")
        consumed = _safe_int(total_tokens)

    if consumed <= 0:
        return None

    ts_raw = obj.get("timestamp")
    dt: Optional[datetime] = None
    if isinstance(ts_raw, str):
        try:
            dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except Exception:
            dt = None
    elif isinstance(ts_raw, (int, float)):
        try:
            dt = datetime.fromtimestamp(float(ts_raw) / 1000)
        except Exception:
            dt = None

    if not dt:
        return None

    day_key = dt.astimezone().strftime("%Y-%m-%d")
    if day_key not in day_keys:
        return None

    return day_key, consumed


def _scan_usage_jsonl(path: Path, start_offset: int, day_keys: set[str]) -> Tuple[int, Dict[str, int]]:
    add_map: Dict[str, int] = {}
    offset = max(0, int(start_offset or 0))

    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = int(f.tell())
            if offset > size:
                offset = 0

            f.seek(offset)
            # 从中间偏移读取时，先丢弃首个残行，避免 JSON 解析噪声
            if offset > 0:
                _ = f.readline()

            while True:
                raw = f.readline()
                if not raw:
                    break
                parsed = _usage_day_and_tokens_from_line(raw.decode("utf-8", errors="replace"), day_keys)
                if not parsed:
                    continue
                day_key, consumed = parsed
                add_map[day_key] = int(add_map.get(day_key, 0)) + int(consumed)

            end_offset = int(f.tell())
            return end_offset, add_map
    except Exception:
        return 0, {}


def _sessions_dirs_stamp() -> Tuple[Tuple[str, int], ...]:
    rows: List[Tuple[str, int]] = []
    for d in AGENTS_DIR.glob("*/sessions"):
        if not d.is_dir():
            continue
        try:
            rows.append((str(d), int(d.stat().st_mtime_ns)))
        except Exception:
            continue
    rows.sort(key=lambda x: x[0])
    return tuple(rows)


def _list_session_jsonl_files() -> List[str]:
    now = time.time()
    dirs_stamp = _sessions_dirs_stamp()

    with _daily_tokens_lock:
        cached_files = _daily_tokens_cache.get("files")
        cached_ts = float(_daily_tokens_cache.get("filesTs", 0) or 0)
        cached_stamp = _daily_tokens_cache.get("dirsStamp")

    if (
        isinstance(cached_files, list)
        and now - cached_ts < _DAILY_FILELIST_TTL_SEC
        and cached_stamp == dirs_stamp
    ):
        return cached_files

    files = glob.glob(str(AGENTS_DIR / "*" / "sessions" / "*.jsonl"))
    with _daily_tokens_lock:
        _daily_tokens_cache.update({"files": files, "filesTs": now, "dirsStamp": dirs_stamp})
    return files


def _build_session_file_passive_map() -> Dict[str, bool]:
    mapping: Dict[str, bool] = {}

    def _looks_like_uuid(value: str) -> bool:
        s = str(value or "").strip().lower()
        if len(s) != 36:
            return False
        parts = s.split("-")
        if [len(p) for p in parts] != [8, 4, 4, 4, 12]:
            return False
        try:
            int("".join(parts), 16)
            return True
        except Exception:
            return False

    def _uuid_from_session_key(value: Any) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        if _looks_like_uuid(raw):
            return raw
        tail = raw.rsplit(":", 1)[-1].strip()
        if _looks_like_uuid(tail):
            return tail
        return ""

    session_files = glob.glob(str(AGENTS_DIR / "*" / "sessions" / "sessions.json"))
    for sf in session_files:
        sf_path = Path(sf)
        agent_id = sf_path.parents[1].name if len(sf_path.parents) >= 2 else ""

        data = _safe_read_json(sf_path, {})
        if not isinstance(data, dict):
            continue

        for session_key, rec in data.items():
            if not isinstance(rec, dict):
                continue

            is_passive = _is_passive_session(str(session_key), rec)

            session_file = str(rec.get("sessionFile") or "").strip()
            if session_file:
                mapping[session_file] = is_passive
                try:
                    mapping[str(Path(session_file).resolve())] = is_passive
                except Exception:
                    pass

            # 回填：sessionFile 缺失（或 sessionFile 与 sessionId 不一致）时，
            # 基于 sessionId/session_key(UUID) 推导默认 jsonl 路径。
            session_uuid = str(rec.get("sessionId") or rec.get("session_id") or "").strip()
            if not _looks_like_uuid(session_uuid):
                session_uuid = _uuid_from_session_key(rec.get("sessionKey") or rec.get("session_key") or session_key)
            if not (agent_id and session_uuid):
                continue

            fallback_path = AGENTS_DIR / agent_id / "sessions" / f"{session_uuid}.jsonl"
            mapping[str(fallback_path)] = is_passive
            try:
                mapping[str(fallback_path.resolve())] = is_passive
            except Exception:
                pass

    return mapping


def _collect_daily_token_series(days: int = 30) -> List[Dict[str, Any]]:
    now = time.time()
    with _daily_tokens_lock:
        rows = _daily_tokens_cache.get("rows")
        ts = float(_daily_tokens_cache.get("ts", 0) or 0)
        if rows is not None and now - ts < _DAILY_TOKENS_TTL_SEC:
            return rows

        cached_day_map = _daily_tokens_cache.get("dayMap")
        cached_day_active_map = _daily_tokens_cache.get("dayActiveMap")
        cached_day_passive_map = _daily_tokens_cache.get("dayPassiveMap")
        cached_file_state = _daily_tokens_cache.get("fileState") or {}
        cached_file_contrib = _daily_tokens_cache.get("fileContrib") or {}
        cached_file_contrib_active = _daily_tokens_cache.get("fileContribActive") or {}
        cached_file_contrib_passive = _daily_tokens_cache.get("fileContribPassive") or {}
        cached_days = int(_daily_tokens_cache.get("days") or 0)
        cached_start_day = str(_daily_tokens_cache.get("startDay") or "")

    today = datetime.now().date()
    days = max(1, int(days))
    start_day = today - timedelta(days=days - 1)
    start_day_s = start_day.strftime("%Y-%m-%d")

    if isinstance(cached_day_map, dict) and cached_days == days and cached_start_day == start_day_s:
        day_map: Dict[str, int] = {k: int(v or 0) for k, v in cached_day_map.items()}
        day_active_map: Dict[str, int] = {
            k: int(v or 0)
            for k, v in (cached_day_active_map.items() if isinstance(cached_day_active_map, dict) else day_map.items())
        }
        day_passive_map: Dict[str, int] = {
            k: int(v or 0)
            for k, v in (cached_day_passive_map.items() if isinstance(cached_day_passive_map, dict) else day_map.items())
        }
        file_state: Dict[str, Dict[str, int]] = {
            str(k): (v if isinstance(v, dict) else {}) for k, v in cached_file_state.items()
        }
        file_contrib: Dict[str, Dict[str, int]] = {
            str(k): {dk: int(dv or 0) for dk, dv in (v or {}).items()}
            for k, v in cached_file_contrib.items()
            if isinstance(v, dict)
        }
        file_contrib_active: Dict[str, Dict[str, int]] = {
            str(k): {dk: int(dv or 0) for dk, dv in (v or {}).items()}
            for k, v in cached_file_contrib_active.items()
            if isinstance(v, dict)
        }
        file_contrib_passive: Dict[str, Dict[str, int]] = {
            str(k): {dk: int(dv or 0) for dk, dv in (v or {}).items()}
            for k, v in cached_file_contrib_passive.items()
            if isinstance(v, dict)
        }
        incremental_mode = True
    else:
        day_map = {(start_day + timedelta(days=i)).strftime("%Y-%m-%d"): 0 for i in range(days)}
        day_active_map = {(start_day + timedelta(days=i)).strftime("%Y-%m-%d"): 0 for i in range(days)}
        day_passive_map = {(start_day + timedelta(days=i)).strftime("%Y-%m-%d"): 0 for i in range(days)}
        file_state = {}
        file_contrib = {}
        file_contrib_active = {}
        file_contrib_passive = {}
        incremental_mode = False

    day_keys = set(day_map.keys())
    file_passive_map = _build_session_file_passive_map()

    jsonl_files = _list_session_jsonl_files()
    live_paths = set(jsonl_files)

    def _rollback_file_contrib(fp: str) -> None:
        prev = file_contrib.pop(fp, {})
        prev_active = file_contrib_active.pop(fp, {})
        prev_passive = file_contrib_passive.pop(fp, {})

        if isinstance(prev, dict):
            for day_key, val in prev.items():
                if day_key in day_map:
                    day_map[day_key] = int(day_map.get(day_key, 0)) - int(val or 0)
        if isinstance(prev_active, dict):
            for day_key, val in prev_active.items():
                if day_key in day_active_map:
                    day_active_map[day_key] = int(day_active_map.get(day_key, 0)) - int(val or 0)
        if isinstance(prev_passive, dict):
            for day_key, val in prev_passive.items():
                if day_key in day_passive_map:
                    day_passive_map[day_key] = int(day_passive_map.get(day_key, 0)) - int(val or 0)

        file_state.pop(fp, None)

    # 增量模式下：文件被删除时先回滚其历史贡献
    if incremental_mode:
        stale_paths = [fp for fp in list(file_contrib.keys()) if fp not in live_paths]
        for fp in stale_paths:
            _rollback_file_contrib(fp)

    for fp in jsonl_files:
        prev_state = file_state.get(fp, {}) if isinstance(file_state.get(fp, {}), dict) else {}
        prev_contrib = file_contrib.get(fp, {}) if isinstance(file_contrib.get(fp, {}), dict) else {}
        prev_contrib_active = file_contrib_active.get(fp, {}) if isinstance(file_contrib_active.get(fp, {}), dict) else {}
        prev_contrib_passive = file_contrib_passive.get(fp, {}) if isinstance(file_contrib_passive.get(fp, {}), dict) else {}

        p = Path(fp)
        try:
            st = p.stat()
        except Exception:
            # 文件在 filelist 之后被删除/轮转：立即回滚旧贡献，避免历史日 overcount 悬挂。
            _rollback_file_contrib(fp)
            continue

        # 初次构建时，跳过明显不在窗口内的历史文件
        if not incremental_mode:
            try:
                if datetime.fromtimestamp(st.st_mtime).date() < start_day:
                    continue
            except Exception:
                pass

        old_inode = int(prev_state.get("inode") or -1)
        old_size = int(prev_state.get("size") or 0)
        old_offset = int(prev_state.get("offset") or 0)
        inode = int(getattr(st, "st_ino", 0) or 0)
        size = int(st.st_size)
        mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))

        can_incremental_read = (
            incremental_mode
            and bool(prev_state)
            and old_inode == inode
            and size >= old_offset
            and size >= old_size
        )

        is_passive_file = bool(file_passive_map.get(fp) or file_passive_map.get(str(p.resolve())))

        if can_incremental_read:
            start_offset = old_offset
            end_offset, add_map = _scan_usage_jsonl(p, start_offset, day_keys)
            if end_offset == 0 and size > 0:
                # 扫描失败时不更新已有状态，避免脏数据覆盖
                continue

            merged = {k: int(v or 0) for k, v in prev_contrib.items()}
            merged_active = {k: int(v or 0) for k, v in prev_contrib_active.items()}
            merged_passive = {k: int(v or 0) for k, v in prev_contrib_passive.items()}
            for day_key, inc in add_map.items():
                inc = int(inc)
                day_map[day_key] = int(day_map.get(day_key, 0)) + inc
                merged[day_key] = int(merged.get(day_key, 0)) + inc
                if is_passive_file:
                    day_passive_map[day_key] = int(day_passive_map.get(day_key, 0)) + inc
                    merged_passive[day_key] = int(merged_passive.get(day_key, 0)) + inc
                else:
                    day_active_map[day_key] = int(day_active_map.get(day_key, 0)) + inc
                    merged_active[day_key] = int(merged_active.get(day_key, 0)) + inc
            file_contrib[fp] = merged
            file_contrib_active[fp] = merged_active
            file_contrib_passive[fp] = merged_passive
            file_state[fp] = {
                "inode": inode,
                "size": size,
                "offset": end_offset,
                "mtimeNs": mtime_ns,
            }
            continue

        # 非增量路径（新文件/轮转/截断）：先回滚旧贡献，再全量重扫该文件
        _rollback_file_contrib(fp)

        end_offset, full_map = _scan_usage_jsonl(p, 0, day_keys)
        if end_offset == 0 and size > 0:
            # 重扫失败时保持“已回滚且缓存已清理”状态，避免悬挂 overcount
            continue

        full_map = {k: int(v or 0) for k, v in full_map.items()}
        active_map = full_map if not is_passive_file else {}
        passive_map = full_map if is_passive_file else {}

        for day_key, val in full_map.items():
            day_map[day_key] = int(day_map.get(day_key, 0)) + int(val)
        for day_key, val in active_map.items():
            day_active_map[day_key] = int(day_active_map.get(day_key, 0)) + int(val)
        for day_key, val in passive_map.items():
            day_passive_map[day_key] = int(day_passive_map.get(day_key, 0)) + int(val)

        file_contrib[fp] = full_map
        file_contrib_active[fp] = {k: int(v) for k, v in active_map.items()}
        file_contrib_passive[fp] = {k: int(v) for k, v in passive_map.items()}
        file_state[fp] = {
            "inode": inode,
            "size": size,
            "offset": end_offset,
            "mtimeNs": mtime_ns,
        }

    # 防御：避免异常回滚导致负值
    for k in list(day_map.keys()):
        day_map[k] = max(0, int(day_map.get(k, 0)))
        day_active_map[k] = max(0, int(day_active_map.get(k, 0)))
        day_passive_map[k] = max(0, int(day_passive_map.get(k, 0)))

    rows = [
        {
            "date": k,
            "tokens": int(day_map.get(k, 0)),
            "activeTokens": int(day_active_map.get(k, 0)),
            "passiveTokens": int(day_passive_map.get(k, 0)),
        }
        for k in sorted(day_map.keys())
    ]
    with _daily_tokens_lock:
        _daily_tokens_cache.update(
            {
                "ts": now,
                "rows": rows,
                "dayMap": day_map,
                "dayActiveMap": day_active_map,
                "dayPassiveMap": day_passive_map,
                "fileState": file_state,
                "fileContrib": file_contrib,
                "fileContribActive": file_contrib_active,
                "fileContribPassive": file_contrib_passive,
                "days": days,
                "startDay": start_day_s,
            }
        )

    return rows


def _collect_models_usage(days: int = 15) -> Dict[str, Any]:
    now = time.time()
    days = max(1, int(days or 1))

    with _models_lock:
        cached = _models_cache.get("data")
        ts = float(_models_cache.get("ts", 0) or 0)
        cached_days = int(_models_cache.get("days") or 0)
        if cached is not None and now - ts < _MODELS_CACHE_TTL_SEC and cached_days == days:
            return cached

    cfg = _safe_read_json(OPENCLAW_CONFIG, {})
    providers = (
        cfg.get("models", {})
        .get("providers", {})
    )

    configured_models: Dict[str, Dict[str, Any]] = {}
    if isinstance(providers, dict):
        for provider_name, provider_cfg in providers.items():
            models = []
            if isinstance(provider_cfg, dict):
                models = provider_cfg.get("models") or []
            if isinstance(models, list):
                for m in models:
                    if not isinstance(m, dict):
                        continue
                    model_id = str(m.get("id") or "").strip()
                    if not model_id:
                        continue
                    configured_models[model_id] = {
                        "model": model_id,
                        "provider": provider_name,
                        "configured": True,
                        "displayName": m.get("name") or model_id,
                    }

    usage: Dict[str, Dict[str, Any]] = {}

    def _ensure_row(model: str, provider: Optional[str]) -> Dict[str, Any]:
        row = usage.setdefault(
            model,
            {
                "model": model,
                "provider": provider,
                "sessions": 0,
                "tokens": 0,
                "activeTokens": 0,
                "passiveTokens": 0,
                "activeSessions": 0,
                "passiveSessions": 0,
                "inputTokens": 0,
                "rawInputTokens": 0,
                "outputTokens": 0,
                "cacheRead": 0,
                "cacheWrite": 0,
                "configured": model in configured_models,
                "_activeSessionSet": set(),
                "_passiveSessionSet": set(),
            },
        )
        if not row.get("provider") and provider:
            row["provider"] = provider
        return row

    today = datetime.now().date()
    start_day = today - timedelta(days=days - 1)
    day_keys = {(start_day + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)}

    file_passive_map = _build_session_file_passive_map()
    jsonl_files = _list_session_jsonl_files()

    for fp in jsonl_files:
        p = Path(fp)
        is_passive_file = bool(file_passive_map.get(fp) or file_passive_map.get(str(p.resolve())))

        try:
            with p.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = (line or "").strip()
                    if not line:
                        continue

                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue

                    if obj.get("type") != "message":
                        continue

                    msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
                    usage_obj = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
                    if not usage_obj:
                        continue

                    ts_raw = obj.get("timestamp")
                    dt: Optional[datetime] = None
                    if isinstance(ts_raw, str):
                        try:
                            dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                        except Exception:
                            dt = None
                    elif isinstance(ts_raw, (int, float)):
                        try:
                            dt = datetime.fromtimestamp(float(ts_raw) / 1000)
                        except Exception:
                            dt = None

                    if not dt:
                        continue

                    day_key = dt.astimezone().strftime("%Y-%m-%d")
                    if day_key not in day_keys:
                        continue

                    raw_input_tokens = _safe_int(usage_obj.get("input"))
                    output_tokens = _safe_int(usage_obj.get("output"))
                    cache_read = _safe_int(usage_obj.get("cacheRead"))
                    cache_write = _safe_int(usage_obj.get("cacheWrite"))

                    has_formula_fields = any(usage_obj.get(k) is not None for k in ("input", "output", "cacheRead"))
                    if has_formula_fields:
                        net_input_tokens = max(0, raw_input_tokens - cache_read)
                        consumed = net_input_tokens + max(0, output_tokens)
                    else:
                        consumed = _safe_int(usage_obj.get("totalTokens"))
                        net_input_tokens = max(0, consumed - max(0, output_tokens))

                    if consumed <= 0 and raw_input_tokens <= 0 and output_tokens <= 0 and cache_read <= 0 and cache_write <= 0:
                        continue

                    model = str(msg.get("model") or "unknown").strip() or "unknown"
                    provider = str(msg.get("provider") or configured_models.get(model, {}).get("provider") or "").strip() or None
                    row = _ensure_row(model, provider)

                    row["tokens"] += consumed
                    row["inputTokens"] += net_input_tokens
                    row["rawInputTokens"] += raw_input_tokens
                    row["outputTokens"] += output_tokens
                    row["cacheRead"] += cache_read
                    row["cacheWrite"] += cache_write

                    if is_passive_file:
                        row["passiveTokens"] += consumed
                        row["_passiveSessionSet"].add(fp)
                    else:
                        row["activeTokens"] += consumed
                        row["_activeSessionSet"].add(fp)
        except Exception:
            continue

    total_active_tokens = 0
    total_passive_tokens = 0
    active_session_set: set[str] = set()
    passive_session_set: set[str] = set()

    for model_id, row in usage.items():
        active_set = row.get("_activeSessionSet") or set()
        passive_set = row.get("_passiveSessionSet") or set()
        if not isinstance(active_set, set):
            active_set = set(active_set)
        if not isinstance(passive_set, set):
            passive_set = set(passive_set)

        row["activeSessions"] = len(active_set)
        row["passiveSessions"] = len(passive_set)
        row["sessions"] = row["activeSessions"] + row["passiveSessions"]
        row["configured"] = bool(row.get("configured") or model_id in configured_models)

        total_active_tokens += int(row.get("activeTokens") or 0)
        total_passive_tokens += int(row.get("passiveTokens") or 0)
        active_session_set.update(active_set)
        passive_session_set.update(passive_set)

        row.pop("_activeSessionSet", None)
        row.pop("_passiveSessionSet", None)

    # 补齐“已配置但未使用”的模型
    for model_id, meta in configured_models.items():
        usage.setdefault(
            model_id,
            {
                "model": model_id,
                "provider": meta.get("provider"),
                "sessions": 0,
                "tokens": 0,
                "activeTokens": 0,
                "passiveTokens": 0,
                "activeSessions": 0,
                "passiveSessions": 0,
                "inputTokens": 0,
                "rawInputTokens": 0,
                "outputTokens": 0,
                "cacheRead": 0,
                "cacheWrite": 0,
                "configured": True,
            },
        )

    rows = sorted(usage.values(), key=lambda r: r.get("tokens", 0), reverse=True)

    # 每日累计处理量：与模型明细使用同一窗口，避免展示口径不一致
    daily_rows = _collect_daily_token_series(days)

    payload = {
        "configuredCount": len(configured_models),
        "usedCount": sum(1 for r in rows if r.get("tokens", 0) > 0),
        "totalTokens": sum(int(r.get("tokens") or 0) for r in rows),
        "activeTokens": int(total_active_tokens),
        "passiveTokens": int(total_passive_tokens),
        "activeSessions": int(len(active_session_set)),
        "passiveSessions": int(len(passive_session_set)),
        "windowDays": int(days),
        "formula": "实际消耗 Token = 净输入 + 输出；净输入 = max(0, 输入 - 缓存复用)（按每次模型调用逐条统计）",
        "models": rows,
        "dailyTokens": daily_rows,
    }

    with _models_lock:
        _models_cache.update({"ts": now, "data": payload, "days": days})
    return payload


def _collect_memory_data(
    status_data: Optional[Dict[str, Any]] = None,
    crons_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    now = time.time()
    with _memory_lock:
        cached = _memory_cache.get("data")
        ts = float(_memory_cache.get("ts", 0) or 0)
        if cached is not None and now - ts < _MEMORY_CACHE_TTL_SEC:
            return cached

    today = datetime.now().strftime("%Y-%m-%d")

    workspace_dirs: List[Path] = []
    for p in sorted(OPENCLAW_DIR.glob("workspace*")):
        if p.is_dir():
            workspace_dirs.append(p)

    workspace_rows: List[Dict[str, Any]] = []
    recent_rows: List[Dict[str, Any]] = []
    total_entries = 0
    today_entries = 0

    for ws in workspace_dirs:
        mem_dir = ws / "memory"
        if not mem_dir.exists() or not mem_dir.is_dir():
            continue

        md_files = [f for f in mem_dir.glob("*.md") if f.is_file()]
        md_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)

        entries = len(md_files)
        today_count = sum(1 for f in md_files if f.name.startswith(today))
        total_entries += entries
        today_entries += today_count

        latest_file = md_files[0] if md_files else None
        latest_at_ms = int(latest_file.stat().st_mtime * 1000) if latest_file else None

        ws_name = ws.name.replace("workspace-", "")
        if ws_name == "workspace":
            ws_name = "main"
        workspace_rows.append(
            {
                "workspace": ws_name,
                "entries": entries,
                "todayEntries": today_count,
                "latestFile": latest_file.name if latest_file else "-",
                "latestAt": latest_at_ms,
            }
        )

        for f in md_files[:8]:
            recent_rows.append(
                {
                    "workspace": ws_name,
                    "file": f.name,
                    "updatedAt": int(f.stat().st_mtime * 1000),
                    "size": int(f.stat().st_size),
                }
            )

    workspace_rows.sort(key=lambda x: x.get("entries", 0), reverse=True)
    recent_rows.sort(key=lambda x: x.get("updatedAt", 0), reverse=True)

    central_dir = OPENCLAW_DIR / "memory-central"
    topics_dir = central_dir / "topics"
    memories_md_dir = central_dir / "memories-md"

    topic_files: List[Path] = []
    if topics_dir.exists():
        for f in topics_dir.rglob("*.md"):
            if "_legacy_" in str(f):
                continue
            if f.is_file():
                topic_files.append(f)

    memories_md_files = [f for f in memories_md_dir.glob("*.md") if f.is_file()] if memories_md_dir.exists() else []

    def _sum_size(paths: List[Path]) -> int:
        s = 0
        for p in paths:
            try:
                s += int(p.stat().st_size)
            except Exception:
                pass
        return s

    memory_root = OPENCLAW_DIR / "memory"
    sqlite_files = [f for f in memory_root.glob("*.sqlite") if f.is_file()] if memory_root.exists() else []
    backups_dir = memory_root / "backups"
    backup_files = [f for f in backups_dir.glob("*.jsonl") if f.is_file()] if backups_dir.exists() else []

    lancedb_size = 0
    lancedb_dir = memory_root / "lancedb-pro"
    if lancedb_dir.exists():
        for f in lancedb_dir.rglob("*"):
            if f.is_file():
                try:
                    lancedb_size += int(f.stat().st_size)
                except Exception:
                    pass

    latest_topic_at = None
    if topic_files:
        latest_topic_at = int(max(f.stat().st_mtime for f in topic_files) * 1000)

    memory_plugin = ((status_data or {}).get("memoryPlugin") or {}) if isinstance(status_data, dict) else {}
    plugin_slot = str(memory_plugin.get("slot") or "memory-lancedb-pro")
    plugin_enabled = bool(memory_plugin.get("enabled", bool(lancedb_size > 0)))

    jobs = (crons_data or {}).get("jobs", []) if isinstance(crons_data, dict) else []

    def _job_row(j: Dict[str, Any], effect: str) -> Dict[str, Any]:
        return {
            "name": j.get("name") or j.get("id") or "-",
            "enabled": bool(j.get("enabled", False)),
            "lastStatus": j.get("lastStatus") or "-",
            "nextRunAtMs": j.get("nextRunAtMs"),
            "lastRunAtMs": j.get("lastRunAtMs"),
            "effect": effect,
        }

    self_improve_rows: List[Dict[str, Any]] = []
    involve_rows: List[Dict[str, Any]] = []

    for j in jobs:
        if not isinstance(j, dict):
            continue
        name = str(j.get("name") or "")
        low = name.lower()

        if (
            "自我升级" in name
            or "evolver" in low
            or "进化" in name
            or "memory-central治理" in name
            or "memory-central" in low
        ):
            self_improve_rows.append(
                _job_row(
                    j,
                    "推动 memory 规则沉淀与索引治理（写入 central topics，append-only）。",
                )
            )

        if (
            "监督" in name
            or "review" in low
            or "介入" in name
            or "involve" in low
        ):
            involve_rows.append(
                _job_row(
                    j,
                    "推动介入流程规则沉淀（触发词/播报规范/退出条件等）。",
                )
            )

    payload = {
        "summary": {
            "workspaceCount": len(workspace_rows),
            "totalEntries": total_entries,
            "todayEntries": today_entries,
            "recentCount": len(recent_rows),
        },
        "workspaces": workspace_rows,
        "recent": recent_rows[:25],
        "central": {
            "exists": central_dir.exists(),
            "hasMemoryMd": (central_dir / "MEMORY.md").exists(),
            "topicsCount": len(topic_files),
            "memoriesMdCount": len(memories_md_files),
            "latestTopicAt": latest_topic_at,
        },
        "storage": {
            "sqliteCount": len(sqlite_files),
            "sqliteBytes": _sum_size(sqlite_files),
            "lancedbBytes": lancedb_size,
            "backupCount": len(backup_files),
        },
        "plugin": {
            "id": plugin_slot,
            "alias": "memory-pro",
            "enabled": plugin_enabled,
            "description": [
                "memory-lancedb-pro（memory-pro）用于长期记忆的存储、检索与治理。",
                "建议仅写入可复用偏好/事实/决策，避免写入密钥与 untrusted metadata 噪声。",
                "关键记忆写入后应使用 memory_recall 进行可检索验证。",
            ],
        },
        "impacts": {
            "selfImprove": {
                "count": len(self_improve_rows),
                "items": self_improve_rows,
            },
            "involve": {
                "count": len(involve_rows),
                "items": involve_rows,
            },
        },
    }

    with _memory_lock:
        _memory_cache.update({"ts": now, "data": payload})
    return payload


def _extract_unfinished_goals_for_job(job: Dict[str, Any], content_text: str) -> List[str]:
    goals: List[str] = []
    text_lc = (content_text or "").lower()

    # 针对监督类任务，补充 monitor-state.json 中的未完成项
    if "monitor-state" in text_lc or "监督" in str(job.get("name") or ""):
        monitor_path = OPENCLAW_DIR / "workspace-review_agent" / "monitor-state.json"
        m = _safe_read_json(monitor_path, {})
        issues = m.get("openIssues", []) if isinstance(m, dict) else []
        if isinstance(issues, list):
            for it in issues:
                if not isinstance(it, dict):
                    continue
                st = str(it.get("status") or "").lower()
                pending = bool(it.get("pendingNeverAction", False))
                if st in {"open", "todo", "pending", "in_progress"} or pending:
                    goals.append(
                        f"- [{it.get('id') or 'issue'}] {it.get('description') or ''}（动作：{it.get('action') or '-'}）"
                    )

    return goals


def _build_cron_monitor_text(job: Dict[str, Any]) -> str:
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}

    msg = ""
    for k in ["text", "message", "prompt", "task", "description", "notes"]:
        v = payload.get(k) if isinstance(payload, dict) else None
        if isinstance(v, str) and v.strip():
            msg = v.strip()
            break
        v2 = job.get(k)
        if isinstance(v2, str) and v2.strip():
            msg = v2.strip()
            break

    if not msg:
        if isinstance(payload, dict) and payload:
            try:
                msg = json.dumps(payload, ensure_ascii=False, indent=2)
            except Exception:
                msg = str(payload)
        else:
            msg = "该任务未配置具体工作内容。"

    goals = _extract_unfinished_goals_for_job(job, msg)

    if goals:
        goal_text = "\n\n当前未完成目标:\n" + "\n".join(goals)
    else:
        goal_text = "\n\n当前未完成目标:\n- 无"

    txt = f"{msg}{goal_text}"
    return txt[:7000]


def _collect_cron_data() -> Dict[str, Any]:
    jobs_payload = _read_json_tolerant(CRON_JOBS_PATH, {"jobs": []})
    jobs = jobs_payload.get("jobs", []) if isinstance(jobs_payload, dict) else []

    running = 0
    normalized_jobs = []

    for job in jobs:
        if not isinstance(job, dict):
            continue
        job_id = str(job.get("id") or "")
        run_file = CRON_RUNS_DIR / f"{job_id}.jsonl"
        last_event = _read_last_jsonl_obj(run_file)

        state = job.get("state", {}) if isinstance(job.get("state"), dict) else {}
        last_status_raw = state.get("lastStatus") or state.get("lastRunStatus") or "-"
        last_status = str(last_status_raw or "").strip().lower()

        active_status = {"running", "started", "processing", "in_progress", "pending", "queued"}
        terminal_status = {"completed", "complete", "success", "succeeded", "ok", "failed", "error", "cancelled", "canceled", "skipped", "timeout", "timed_out", "done"}

        # 数据准确优先：先用 jobs.json 的状态作为主判据，run jsonl 只做补充
        if last_status in active_status:
            is_running = True
        elif last_status in terminal_status:
            is_running = False
        else:
            is_running = False
            if isinstance(last_event, dict):
                event_hint = str(last_event.get("action") or last_event.get("status") or "").lower()
                if event_hint in active_status:
                    is_running = True
                elif event_hint in terminal_status:
                    is_running = False

        if is_running:
            running += 1

        normalized_jobs.append(
            {
                "id": job_id,
                "name": job.get("name") or job_id,
                "agentId": job.get("agentId") or "-",
                "enabled": bool(job.get("enabled", False)),
                "running": is_running,
                "lastStatus": last_status_raw,
                "lastRunAtMs": state.get("lastRunAtMs"),
                "nextRunAtMs": state.get("nextRunAtMs"),
                "lastDurationMs": state.get("lastDurationMs"),
                "schedule": job.get("schedule") or {},
            }
        )

    normalized_jobs.sort(
        key=lambda x: (not x.get("enabled", False), x.get("nextRunAtMs") or 10**18)
    )

    enabled_count = sum(1 for j in normalized_jobs if j["enabled"])
    disabled_count = len(normalized_jobs) - enabled_count

    return {
        "total": len(normalized_jobs),
        "enabled": enabled_count,
        "disabled": disabled_count,
        "running": running,
        "jobs": normalized_jobs,
    }


def _get_cron_monitor_text(job_id: str) -> str:
    jobs_payload = _read_json_tolerant(CRON_JOBS_PATH, {"jobs": []})
    jobs = jobs_payload.get("jobs", []) if isinstance(jobs_payload, dict) else []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if str(job.get("id") or "") == str(job_id):
            return _build_cron_monitor_text(job)
    return "未找到该任务，可能已被删除或重命名。"


def _build_channels_from_status(status_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    lines = status_data.get("channelSummary") or []
    channels: List[Dict[str, Any]] = []

    for ln in lines:
        if not isinstance(ln, str):
            continue
        if ":" not in ln:
            continue
        if ln.startswith("  -"):
            continue
        name, tail = ln.split(":", 1)
        state_txt = tail.strip().lower()
        channels.append(
            {
                "name": name.strip().lower(),
                "label": name.strip(),
                "state": state_txt,
                "configured": "configured" in state_txt,
            }
        )
    return channels


def _collect_agents_data(status_data: Dict[str, Any], subagents: Dict[str, Any]) -> Dict[str, Any]:
    agents_block = status_data.get("agents", {}) if isinstance(status_data, dict) else {}
    heartbeat_block = status_data.get("heartbeat", {}) if isinstance(status_data, dict) else {}

    hb_map: Dict[str, Dict[str, Any]] = {}
    for row in heartbeat_block.get("agents", []) if isinstance(heartbeat_block, dict) else []:
        if isinstance(row, dict) and row.get("agentId"):
            hb_map[str(row["agentId"])] = row

    sub_runs = subagents.get("runs", []) if isinstance(subagents, dict) else []

    result_agents: List[Dict[str, Any]] = []
    running_count = 0

    for a in agents_block.get("agents", []) if isinstance(agents_block, dict) else []:
        if not isinstance(a, dict):
            continue
        aid = str(a.get("id") or "")
        age_ms = a.get("lastActiveAgeMs")
        agent_running = isinstance(age_ms, (int, float)) and age_ms <= ACTIVE_AGENT_WINDOW_MS

        own_subs = []
        for s in sub_runs:
            if not isinstance(s, dict):
                continue
            parent = s.get("parentAgentId") or ""
            sk = s.get("sessionKey") or ""
            if parent == aid or f"agent:{aid}:" in str(sk):
                own_subs.append(s)

        sub_running = sum(
            1
            for s in own_subs
            if str(s.get("status", "")).lower()
            in {"running", "active", "started", "in_progress", "processing"}
        )

        if agent_running or sub_running > 0:
            running_count += 1

        hb = hb_map.get(aid, {})
        result_agents.append(
            {
                "id": aid,
                "name": a.get("name") or aid,
                "workspaceDir": a.get("workspaceDir"),
                "sessionsCount": int(a.get("sessionsCount") or 0),
                "lastUpdatedAt": a.get("lastUpdatedAt"),
                "lastActiveAgeMs": age_ms,
                "running": bool(agent_running),
                "subagentsTotal": len(own_subs),
                "subagentsRunning": sub_running,
                "heartbeatEnabled": bool(hb.get("enabled", False)),
                "heartbeatEvery": hb.get("every") or "disabled",
            }
        )

    result_agents.sort(key=lambda x: (not x["running"], x["id"]))

    return {
        "total": len(result_agents),
        "running": running_count,
        "agents": result_agents,
    }


def _collect_openclaw_summary(status_data: Optional[Dict[str, Any]], status_err: Optional[str]) -> Dict[str, Any]:
    if not status_data:
        return {
            "ok": False,
            "state": "离线",
            "error": status_err or "无法获取 OpenClaw 状态",
            "gateway": {},
            "security": {},
            "update": {},
            "channels": [],
        }

    gateway = status_data.get("gateway") or {}
    gateway_service = status_data.get("gatewayService") or {}
    security = status_data.get("securityAudit") or {}
    update = status_data.get("update") or {}

    critical = int((security.get("summary") or {}).get("critical") or 0)
    warns = int((security.get("summary") or {}).get("warn") or 0)

    gateway_reachable = bool(gateway.get("reachable", False))
    runtime_short = str(gateway_service.get("runtimeShort") or "")
    service_running = "running" in runtime_short.lower()

    runtime_lc = runtime_short.lower()
    restarting_signals = (
        "activating",
        "starting",
        "restarting",
        "reload",
        "draining",
        "deactivating",
    )

    # Never 要求只保留 3 态：正常 / 重启 / 离线
    if not gateway_reachable:
        state = "离线"
    elif any(sig in runtime_lc for sig in restarting_signals):
        state = "重启"
    elif service_running:
        state = "正常"
    else:
        state = "离线"

    channels = _build_channels_from_status(status_data)

    return {
        "ok": True,
        "state": state,
        "gateway": {
            "reachable": gateway_reachable,
            "latencyMs": gateway.get("connectLatencyMs"),
            "url": gateway.get("url"),
            "service": runtime_short,
        },
        "security": {
            "critical": critical,
            "warn": warns,
            "info": int((security.get("summary") or {}).get("info") or 0),
        },
        "update": {
            "latestVersion": (update.get("registry") or {}).get("latestVersion"),
            "installKind": update.get("installKind"),
        },
        "channels": channels,
        "error": status_err,
    }


def _build_dashboard_payload() -> Dict[str, Any]:
    status_data, status_err = _run_openclaw_status()
    status_ts_ms = _cache_ts_ms(_status_cache, _status_lock)

    subagents = _collect_subagent_runs()
    subagents_ts_ms = int(time.time() * 1000)

    agents = _collect_agents_data(status_data or {}, subagents)
    agents_ts_ms = int(time.time() * 1000)

    crons = _collect_cron_data()
    crons_ts_ms = int(time.time() * 1000)

    models = _collect_models_usage()
    models_ts_ms = _cache_ts_ms(_models_cache, _models_lock) or int(time.time() * 1000)

    memory_data = _collect_memory_data(status_data=status_data or {}, crons_data=crons)
    memory_ts_ms = _cache_ts_ms(_memory_cache, _memory_lock) or int(time.time() * 1000)

    openclaw_summary = _collect_openclaw_summary(status_data, status_err)
    openclaw_ts_ms = status_ts_ms or int(time.time() * 1000)

    sessions = (status_data or {}).get("sessions", {})
    recent_sessions = sessions.get("recent", []) if isinstance(sessions, dict) else []

    main_model = "unknown"
    main_tokens = 0
    for rs in recent_sessions:
        if not isinstance(rs, dict):
            continue
        if rs.get("agentId") == "main":
            main_model = str(rs.get("model") or main_model)
            main_tokens = int(rs.get("totalTokens") or 0)
            break

    generated_at_ms = int(time.time() * 1000)
    source_timestamps = {
        "status": status_ts_ms,
        "openclaw": openclaw_ts_ms,
        "agents": agents_ts_ms,
        "subagents": subagents_ts_ms,
        "crons": crons_ts_ms,
        "models": models_ts_ms,
        "memory": memory_ts_ms,
    }
    source_lags_ms = {
        k: (max(0, generated_at_ms - int(v)) if isinstance(v, (int, float)) else None)
        for k, v in source_timestamps.items()
    }

    payload = {
        "title": APP_TITLE,
        "generatedAt": generated_at_ms,
        "sourceTimestamps": source_timestamps,
        "sourceLagsMs": source_lags_ms,
        "overview": {
            "agentCount": agents["total"],
            "runningAgents": agents["running"],
            "subagentCount": subagents["total"],
            "runningSubagents": subagents["running"],
            "cronCount": crons["total"],
            "runningCrons": crons["running"],
            "modelCount": models["configuredCount"],
            "tokens": models["totalTokens"],
            "model": main_model,
            "mainTokens": main_tokens,
            "status": openclaw_summary.get("state"),
        },
        "agents": agents,
        "subagents": subagents,
        "crons": crons,
        "models": models,
        "memory": memory_data,
        "openclaw": openclaw_summary,
        "sessions": sessions,
        "usage": (status_data or {}).get("usage") or {},
    }
    return payload


def _empty_dashboard_payload() -> Dict[str, Any]:
    now_ms = int(time.time() * 1000)
    return {
        "title": APP_TITLE,
        "generatedAt": now_ms,
        "sourceTimestamps": {
            "status": None,
            "openclaw": None,
            "agents": None,
            "subagents": None,
            "crons": None,
            "models": None,
            "memory": None,
        },
        "sourceLagsMs": {
            "status": None,
            "openclaw": None,
            "agents": None,
            "subagents": None,
            "crons": None,
            "models": None,
            "memory": None,
        },
        "overview": {
            "agentCount": 0,
            "runningAgents": 0,
            "subagentCount": 0,
            "runningSubagents": 0,
            "cronCount": 0,
            "runningCrons": 0,
            "modelCount": 0,
            "tokens": 0,
            "model": "-",
            "mainTokens": 0,
            "status": "加载中",
        },
        "agents": {"total": 0, "running": 0, "agents": []},
        "subagents": {"total": 0, "running": 0, "runs": []},
        "crons": {"total": 0, "enabled": 0, "disabled": 0, "running": 0, "jobs": []},
        "models": {
            "configuredCount": 0,
            "usedCount": 0,
            "totalTokens": 0,
            "activeTokens": 0,
            "passiveTokens": 0,
            "activeSessions": 0,
            "passiveSessions": 0,
            "formula": "实际消耗 Token = 净输入 + 输出；净输入 = max(0, 输入 - 缓存复用)",
            "models": [],
            "dailyTokens": [],
        },
        "memory": {
            "summary": {"workspaceCount": 0, "totalEntries": 0, "todayEntries": 0, "recentCount": 0},
            "workspaces": [],
            "recent": [],
            "central": {"exists": False, "hasMemoryMd": False, "topicsCount": 0, "memoriesMdCount": 0, "latestTopicAt": None},
            "storage": {"sqliteCount": 0, "sqliteBytes": 0, "lancedbBytes": 0, "backupCount": 0},
            "plugin": {"id": "memory-lancedb-pro", "alias": "memory-pro", "enabled": False, "description": []},
            "impacts": {"selfImprove": {"count": 0, "items": []}, "involve": {"count": 0, "items": []}},
        },
        "openclaw": {"ok": False, "state": "加载中", "error": "warming up", "gateway": {}, "security": {}, "update": {}, "channels": []},
        "sessions": {},
        "usage": {},
    }


def _refresh_dashboard_cache_sync() -> None:
    global _dash_refreshing
    try:
        data = _build_dashboard_payload()
        with _dash_lock:
            _dash_cache.update({"ts": time.time(), "data": data})
    finally:
        with _dash_lock:
            _dash_refreshing = False


def _start_dashboard_refresh() -> None:
    global _dash_refreshing
    with _dash_lock:
        if _dash_refreshing:
            return
        _dash_refreshing = True
    t = threading.Thread(target=_refresh_dashboard_cache_sync, daemon=True, name="clawstatus-dash-refresh")
    t.start()


def _get_dashboard_payload() -> Dict[str, Any]:
    now = time.time()
    with _dash_lock:
        cached = _dash_cache.get("data")
        cached_ts = float(_dash_cache.get("ts", 0) or 0)

    # 首次无缓存：立即返回占位数据，后台异步构建，保证“秒开”体感
    if cached is None:
        _start_dashboard_refresh()
        return _empty_dashboard_payload()

    # 缓存命中：直接返回
    if now - cached_ts < _DASH_CACHE_TTL_SEC:
        return cached

    # 缓存过期：返回旧数据并异步刷新，避免请求阻塞
    _start_dashboard_refresh()
    return cached


def create_app() -> Flask:
    app = Flask(__name__)
    # Never 要求：页面开箱即用，不需要任何 token 输入
    required_token = None

    @app.after_request
    def _disable_cache(resp):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp

    # 后台预热 openclaw status，避免 API 首次请求卡住
    global _bg_warmup_started
    if not _bg_warmup_started:
        t = threading.Thread(target=_bg_status_warmup_loop, daemon=True, name="clawstatus-warmup")
        t.start()
        _bg_warmup_started = True

    # 仪表盘数据预热（异步），避免首次接口阻塞
    _start_dashboard_refresh()

    @app.get("/")
    def index() -> Response:
        return Response(_index_html(required_token), mimetype="text/html")

    @app.get("/api/auth/check")
    def api_auth_check():
        valid = _is_authorized(required_token)
        # 未配置 token 时视为 valid
        if not required_token:
            valid = True
        return jsonify(
            {
                "valid": valid,
                "requiresAuth": bool(required_token),
                "project": APP_TITLE,
                "version": __version__,
            }
        )

    @app.get("/api/dashboard")
    def api_dashboard():
        auth_resp = _require_auth(required_token)
        if auth_resp is not None:
            return auth_resp
        return jsonify(_get_dashboard_payload())

    @app.get("/api/overview")
    def api_overview():
        auth_resp = _require_auth(required_token)
        if auth_resp is not None:
            return auth_resp
        data = _get_dashboard_payload()
        out = dict(data.get("overview", {}))
        out["_sourceLagsMs"] = data.get("sourceLagsMs", {})
        out["_sourceTimestamps"] = data.get("sourceTimestamps", {})
        return jsonify(out)

    @app.get("/api/agents")
    def api_agents():
        auth_resp = _require_auth(required_token)
        if auth_resp is not None:
            return auth_resp
        data = _get_dashboard_payload()
        out = dict(data.get("agents", {}))
        out["_sourceLagsMs"] = data.get("sourceLagsMs", {})
        out["_sourceTimestamps"] = data.get("sourceTimestamps", {})
        return jsonify(out)

    @app.get("/api/crons")
    def api_crons():
        auth_resp = _require_auth(required_token)
        if auth_resp is not None:
            return auth_resp
        data = _get_dashboard_payload()
        out = dict(data.get("crons", {}))
        out["_sourceLagsMs"] = data.get("sourceLagsMs", {})
        out["_sourceTimestamps"] = data.get("sourceTimestamps", {})
        return jsonify(out)

    @app.get("/api/cron-monitor/<job_id>")
    def api_cron_monitor(job_id: str):
        auth_resp = _require_auth(required_token)
        if auth_resp is not None:
            return auth_resp
        text = _get_cron_monitor_text(job_id)
        return jsonify({"id": job_id, "monitorText": text})

    @app.get("/api/openclaw")
    def api_openclaw():
        auth_resp = _require_auth(required_token)
        if auth_resp is not None:
            return auth_resp
        data = _get_dashboard_payload()
        out = dict(data.get("openclaw", {}))
        out["_sourceLagsMs"] = data.get("sourceLagsMs", {})
        out["_sourceTimestamps"] = data.get("sourceTimestamps", {})
        return jsonify(out)

    @app.get("/api/models")
    def api_models():
        auth_resp = _require_auth(required_token)
        if auth_resp is not None:
            return auth_resp
        data = _get_dashboard_payload()
        out = dict(data.get("models", {}))
        out["_sourceLagsMs"] = data.get("sourceLagsMs", {})
        out["_sourceTimestamps"] = data.get("sourceTimestamps", {})
        return jsonify(out)

    @app.get("/api/memory")
    def api_memory():
        auth_resp = _require_auth(required_token)
        if auth_resp is not None:
            return auth_resp
        data = _get_dashboard_payload()
        out = dict(data.get("memory", {}))
        out["_sourceLagsMs"] = data.get("sourceLagsMs", {})
        out["_sourceTimestamps"] = data.get("sourceTimestamps", {})
        return jsonify(out)

    # ---- 兼容旧 API（供现有脚本/测试使用） ----
    @app.get("/api/health")
    def api_health():
        auth_resp = _require_auth(required_token)
        if auth_resp is not None:
            return auth_resp
        status_data, status_err = _run_openclaw_status()
        checks = [
            {"name": "openclaw_status", "ok": status_data is not None, "error": status_err},
            {"name": "config", "ok": OPENCLAW_CONFIG.exists(), "path": str(OPENCLAW_CONFIG)},
        ]
        ok = all(c.get("ok") for c in checks)
        return jsonify({"ok": ok, "checks": checks, "time": int(time.time() * 1000)})

    @app.get("/api/system-health")
    def api_system_health():
        auth_resp = _require_auth(required_token)
        if auth_resp is not None:
            return auth_resp
        data = _get_dashboard_payload()
        return jsonify(data.get("openclaw", {}))

    @app.get("/api/channels")
    def api_channels():
        auth_resp = _require_auth(required_token)
        if auth_resp is not None:
            return auth_resp
        data = _get_dashboard_payload()
        return jsonify({"channels": data.get("openclaw", {}).get("channels", [])})

    @app.get("/api/channel/<channel>")
    def api_channel_single(channel: str):
        auth_resp = _require_auth(required_token)
        if auth_resp is not None:
            return auth_resp
        data = _get_dashboard_payload()
        channels = data.get("openclaw", {}).get("channels", [])
        current = next((c for c in channels if c.get("name") == channel.lower()), None)
        payload = {
            "channel": channel,
            "configured": bool(current and current.get("configured")),
            "state": (current or {}).get("state", "unknown"),
            "messages": [],
        }
        if channel.lower() in {"telegram", "imessage"}:
            payload["todayIn"] = 0
            payload["todayOut"] = 0
        return jsonify(payload)

    @app.get("/api/sessions")
    def api_sessions():
        auth_resp = _require_auth(required_token)
        if auth_resp is not None:
            return auth_resp
        data = _get_dashboard_payload()
        return jsonify(data.get("sessions", {}))

    @app.get("/api/usage")
    def api_usage():
        auth_resp = _require_auth(required_token)
        if auth_resp is not None:
            return auth_resp
        data = _get_dashboard_payload()
        return jsonify({"usage": data.get("usage", {}), "models": data.get("models", {})})

    @app.get("/api/transcripts")
    def api_transcripts():
        auth_resp = _require_auth(required_token)
        if auth_resp is not None:
            return auth_resp
        return jsonify({"transcripts": []})

    @app.get("/api/subagents")
    def api_subagents():
        auth_resp = _require_auth(required_token)
        if auth_resp is not None:
            return auth_resp
        data = _get_dashboard_payload()
        return jsonify(data.get("subagents", {}))

    return app


def _index_html(auth_token: Optional[str] = None) -> str:
    return f"""<!doctype html>
<html lang="zh-CN" data-bs-theme="dark">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{APP_TITLE}</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css" />
  <style>
    :root {{
      --bg: #05070d;
      --bg2: #0f172a;
      --card: #0b1220;
      --text: #e5e7eb;
      --muted: #94a3b8;
      --ok: #22c55e;
      --warn: #f59e0b;
      --bad: #f87171;
      --line: #1f2937;
      --accent: #60a5fa;
      --accent-soft: #111827;
      --passive: #f59f00;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Helvetica, Arial, "PingFang SC", "Microsoft Yahei", sans-serif;
    }}
    header {{
      padding: 14px 24px;
      border-bottom: 1px solid var(--line);
      position: sticky;
      top: 0;
      background: rgba(5, 7, 13, 0.92);
      backdrop-filter: blur(8px);
      z-index: 10;
      display: grid;
      grid-template-columns: auto 1fr auto;
      align-items: center;
      gap: 12px;
    }}
    h1 {{ margin: 0; font-size: 22px; font-weight: 700; justify-self: start; }}
    .wrap {{ padding: 14px 24px 28px; max-width: 1500px; margin: 0 auto; }}
    .nav {{ display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 0; justify-content: center; }}
    .site-nav {{ display:flex; gap:8px; justify-self:end; }}
    .site-link {{ display:inline-flex; align-items:center; border:1px solid var(--line); background: var(--card); color: var(--text); border-radius: 8px; padding: 8px 12px; font-size:13px; text-decoration:none; }}
    .site-link:hover {{ border-color: var(--accent); color: var(--accent); background: var(--accent-soft); }}
    .nav-tab {{ border: 1px solid var(--line); background: var(--card); color: var(--text); border-radius: 8px; padding: 8px 12px; cursor: pointer; font-size: 13px; }}
    .nav-tab.active {{ border-color: var(--accent); background: var(--accent-soft); color: var(--accent); }}
    .page {{ display: none; }}
    .page.active {{ display: block; }}
    .grid {{ display: grid; grid-template-columns: repeat(4, minmax(180px,1fr)); gap: 12px; }}
    .compact-grid {{ grid-template-columns: repeat(5, minmax(150px, 1fr)); gap: 10px; }}
    .compact-grid .card {{ padding: 12px; }}
    .compact-grid .v {{ font-size: 22px; }}
    .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 14px; }}
    .k {{ color: var(--muted); font-size: 12px; }}
    .v {{ font-size: 24px; font-weight: 700; margin-top: 6px; }}
    .v.sm {{ font-size: 18px; }}
    .ok {{ color: var(--ok); }} .warn {{ color: var(--warn); }} .bad {{ color: var(--bad); }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
    th, td {{ text-align: left; padding: 10px 8px; border-bottom: 1px solid var(--line); font-size: 13px; vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 600; position: sticky; top: 0; background: var(--card); }}
    .panel {{ margin-top: 14px; background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 12px; }}
    .meta {{ color: var(--muted); font-size: 12px; margin-top: 10px; }}
    .pill {{ display:inline-block; border:1px solid var(--line); border-radius:999px; padding:2px 8px; font-size:11px; color:var(--muted); margin-left: 6px; background: var(--bg2); }}
    .monitor-grid {{ margin-top: 10px; display: grid; grid-template-columns: 1fr 2fr; gap: 12px; }}
    .monitor-box {{ border:1px solid var(--line); border-radius:10px; background:var(--card); padding:10px; min-height: 180px; }}
    .monitor-title {{ font-size:13px; color:var(--muted); margin-bottom:8px; }}
    .monitor-list-item {{ width: 100%; text-align: left; border: 1px solid var(--line); background: var(--bg2); color: var(--text); border-radius: 8px; padding: 8px 10px; margin-bottom: 8px; cursor: pointer; }}
    .monitor-list-item.active {{ border-color: var(--accent); background: var(--accent-soft); color: var(--accent); }}
    .monitor-content {{ white-space: pre-wrap; font-size:12px; line-height:1.5; max-height:300px; overflow:auto; color: var(--text); }}
    .btn-monitor {{ border:1px solid var(--line); background:var(--bg2); color:var(--text); border-radius:8px; padding:4px 10px; font-size:12px; cursor:pointer; }}
    .btn-monitor:hover {{ border-color: var(--accent); color: var(--accent); background: var(--accent-soft); }}
    .daily-chart {{ display:flex; align-items:flex-end; justify-content:space-between; gap:10px; min-height:240px; padding: 10px 6px 0; overflow: hidden; }}
    .daily-item {{ flex: 1 1 0; min-width: 44px; max-width: 72px; text-align:center; }}
    .daily-bar-wrap {{ height: 160px; display:flex; align-items:flex-end; justify-content:center; }}
    .daily-bar-stack {{ width: 100%; max-width: 42px; border-radius: 8px 8px 0 0; overflow: hidden; display:flex; flex-direction: column-reverse; background: #0f172a; border: 1px solid var(--line); }}
    .daily-bar-active {{ background: #3b82f6; width: 100%; min-height: 0; }}
    .daily-bar-passive {{ background: var(--passive); width: 100%; min-height: 0; }}
    .daily-bar-passive.has-value {{ min-height: 3px; }}
    .daily-value {{ color: var(--muted); font-size: 10px; margin-bottom: 6px; white-space: nowrap; }}
    .daily-label {{ color: var(--muted); font-size: 10px; margin-top: 6px; white-space: nowrap; }}
    .daily-legend {{ display: flex; gap: 12px; align-items: center; margin-top: 8px; color: var(--muted); font-size: 12px; }}
    .legend-dot {{ width: 10px; height: 10px; border-radius: 50%; display: inline-block; margin-right: 4px; }}
    .daily-list {{ margin-top: 12px; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }}
    #flow-svg {{ width: 100%; height: 240px; border: 1px solid var(--line); border-radius: 8px; background: var(--card); }}
    #boot-overlay {{ display: none; }}
    @media (max-width: 1200px) {{ header {{ grid-template-columns: 1fr; }} h1 {{ justify-self: center; }} .nav {{ justify-content: center; }} .site-nav {{ justify-self: center; }} }}
    @media (max-width: 1000px) {{ .grid {{ grid-template-columns: repeat(2,minmax(160px,1fr)); }} .monitor-grid {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <header>
    <h1>{APP_TITLE}</h1>
    <div class="nav">
      <button class="nav-tab active" data-page="overview">Overview</button>
      <button class="nav-tab" data-page="flow">Flow</button>
      <button class="nav-tab" data-page="crons">Crons</button>
      <button class="nav-tab" data-page="usage">Tokens</button>
      <button class="nav-tab" data-page="memory">Memory</button>
    </div>
    <nav class="site-nav" aria-label="站点导航">
      <a class="site-link" href="https://never.ink" target="_blank" rel="noopener noreferrer">博客</a>
      <a class="site-link" href="#">摄影</a>
    </nav>
  </header>
  <div class="wrap">

    <section id="page-overview" class="page active">
      <div class="grid compact-grid" id="cards"></div>
      <div class="panel">
        <h3 style="margin:4px 0 8px">Agent 概览 <span class="pill">含子 Agent</span><span class="pill" id="cd-agents">60秒后刷新</span></h3>
        <table class="table table-hover align-middle mb-0">
          <thead><tr><th>Agent</th><th>运行</th><th>Sub-Agent</th><th>Sessions</th><th>Heartbeat</th></tr></thead>
          <tbody id="agents-body"></tbody>
        </table>
      </div>
    </section>

    <section id="page-flow" class="page">
      <div class="panel">
        <h3 style="margin:4px 0 8px">全局流转关系（简图）</h3>
        <svg id="flow-svg" viewBox="0 0 1200 240" xmlns="http://www.w3.org/2000/svg">
          <rect x="20" y="70" width="180" height="100" rx="10" fill="#0f172a" stroke="#334155" />
          <text x="42" y="125" fill="#e5e7eb" font-size="20">OpenClaw</text>
          <g><rect x="300" y="20" width="180" height="70" rx="10" fill="#0f172a" stroke="#334155"/><text x="340" y="63" fill="#e5e7eb" font-size="18">Agents</text></g>
          <g><rect x="300" y="150" width="180" height="70" rx="10" fill="#0f172a" stroke="#334155"/><text x="345" y="193" fill="#e5e7eb" font-size="18">Crons</text></g>
          <g><rect x="590" y="20" width="180" height="70" rx="10" fill="#0f172a" stroke="#334155"/><text x="622" y="63" fill="#e5e7eb" font-size="18">Models</text></g>
          <g><rect x="590" y="150" width="180" height="70" rx="10" fill="#0f172a" stroke="#334155"/><text x="624" y="193" fill="#e5e7eb" font-size="18">Usage</text></g>
          <path d="M200 120 L300 55" stroke="#64748b" stroke-width="2" fill="none" />
          <path d="M200 120 L300 185" stroke="#64748b" stroke-width="2" fill="none" />
          <path d="M480 55 L590 55" stroke="#64748b" stroke-width="2" fill="none" />
          <path d="M480 185 L590 185" stroke="#64748b" stroke-width="2" fill="none" />
        </svg>
      </div>
    </section>

    <section id="page-crons" class="page">
      <div class="panel">
        <h3 style="margin:4px 0 8px">Cron 任务 <span class="pill" id="cd-crons">60秒后刷新</span></h3>
        <div class="monitor-grid" id="cron-monitor-panels">
          <div class="monitor-box">
            <div class="monitor-title">当前监督任务</div>
            <div id="cron-monitor-list"></div>
          </div>
          <div class="monitor-box">
            <div class="monitor-title" id="cron-monitor-detail-title">执行内容</div>
            <div class="monitor-content" id="cron-monitor-detail">请选择左侧任务查看执行内容。</div>
          </div>
        </div>
        <table class="table table-hover align-middle mb-0">
          <thead><tr><th>任务</th><th>Agent</th><th>启用</th><th>运行中</th><th>上次状态</th><th>上次耗时</th><th>下次执行</th><th>操作</th></tr></thead>
          <tbody id="crons-body"></tbody>
        </table>
      </div>
    </section>

    <section id="page-usage" class="page">
      <div class="panel">
        <h3 style="margin:4px 0 8px">每日 Token 实际消耗图（最近15天） <span class="pill" id="cd-tokens">60秒后刷新</span></h3>
        <div id="daily-token-meta" class="meta">加载中…</div>
        <div class="daily-legend">
          <span><i class="legend-dot" style="background:#2563eb"></i>主动消耗</span>
          <span><i class="legend-dot" style="background:#f59f00"></i>被动消耗</span>
        </div>
        <div id="daily-token-chart" class="meta">加载中…</div>
      </div>
      <div class="panel">
        <h3 style="margin:4px 0 8px">模型 Token 实际消耗</h3>
        <div id="token-consumption-meta" class="meta">加载中…</div>
        <table class="table table-hover align-middle mb-0">
          <thead><tr><th>Model</th><th>Provider</th><th>Sessions</th><th>总消耗</th><th>主动消耗</th><th>被动消耗</th><th>Input(净/总)</th><th>Output</th><th>Cache(R/W)</th></tr></thead>
          <tbody id="models-body"></tbody>
        </table>
      </div>
    </section>

    <section id="page-memory" class="page">
      <div class="grid" id="memory-cards"></div>
      <div class="panel">
        <h3 style="margin:4px 0 8px">memory-pro 插件说明</h3>
        <div id="memory-plugin-desc" class="monitor-content">加载中…</div>
      </div>
      <div class="panel">
        <h3 style="margin:4px 0 8px">self-improve / involve 对 memory 的影响</h3>
        <div id="memory-impact" class="monitor-content">加载中…</div>
      </div>
      <div class="panel">
        <h3 style="margin:4px 0 8px">Memory 工作区概览 <span class="pill" id="cd-memory">60秒后刷新</span></h3>
        <table class="table table-hover align-middle mb-0">
          <thead><tr><th>Workspace</th><th>条目数</th><th>今日新增</th><th>最新文件</th><th>更新时间</th></tr></thead>
          <tbody id="memory-workspaces-body"></tbody>
        </table>
      </div>
      <div class="panel">
        <h3 style="margin:4px 0 8px">最近 Memory 文件</h3>
        <table class="table table-hover align-middle mb-0">
          <thead><tr><th>Workspace</th><th>文件</th><th>大小</th><th>更新时间</th></tr></thead>
          <tbody id="memory-recent-body"></tbody>
        </table>
      </div>
    </section>
  </div>

  <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/js/bootstrap.bundle.min.js"></script>
  <script>
    const SERVER_TOKEN = '{auth_token or ''}';
    const $ = (s) => document.querySelector(s);
    const fmtNum = (n) => (n ?? 0).toLocaleString();
    const fmtM = (n) => {{
      const v = Number(n || 0) / 1_000_000;
      if (v >= 100) return v.toFixed(0) + 'M';
      if (v >= 10) return v.toFixed(1) + 'M';
      return v.toFixed(2) + 'M';
    }};
    const fmtTime = (ms) => {{
      if (!ms) return '-';
      const d = new Date(ms);
      return d.toLocaleString();
    }};
    const fmtLag = (ms) => {{
      const v = Number(ms || 0);
      if (!Number.isFinite(v) || v < 0) return '-';
      if (v < 1000) return Math.round(v) + 'ms';
      const s = v / 1000;
      if (s < 60) return s.toFixed(1) + 's';
      const m = Math.floor(s / 60);
      const rs = Math.round(s % 60);
      return `${{m}}m${{rs}}s`;
    }};

    function stateBadge(v) {{
      const s = String(v ?? '').toLowerCase();
      if (v === true || s === 'true' || s === 'running' || s === 'healthy' || s === '正常') return '<span class="ok">●</span>';
      if (s === '重启' || s.includes('restart') || s.includes('starting') || s.includes('activating')) return '<span class="warn">●</span>';
      return '<span class="bad">●</span>';
    }}

    function token() {{
      return SERVER_TOKEN || localStorage.getItem('clawstatus-token') || '';
    }}

    async function fetchJson(url, headers) {{
      const r = await fetch(url, {{ headers }});
      if (!r.ok) throw new Error(url + ' -> HTTP ' + r.status);
      return r.json();
    }}

    const dashboardState = {{
      generatedAt: Date.now(),
      sourceTimestamps: {{}},
      sourceLagsMs: {{}},
      overview: {{}},
      agents: {{}},
      crons: {{}},
      openclaw: {{}},
      models: {{}},
      memory: {{}},
    }};

    const DASH_SNAPSHOT_KEY = 'clawstatus-dashboard-snapshot-v2';
    const VALID_PAGES = new Set(['overview', 'flow', 'crons', 'usage', 'memory']);
    let activePage = 'overview';

    const refreshState = {{
      overview: {{ url: '/api/overview', interval: 15, remain: 1, page: 'overview' }},
      openclaw: {{ url: '/api/openclaw', interval: 30, remain: 2, page: 'overview' }},
      agents: {{ url: '/api/agents', interval: 30, remain: 3, page: 'overview' }},
      crons: {{ url: '/api/crons', interval: 60, remain: 4, page: 'crons' }},
      models: {{ url: '/api/models', interval: 120, remain: 6, page: 'usage' }},
      memory: {{ url: '/api/memory', interval: 120, remain: 8, page: 'memory' }},
    }};

    const refreshingKeys = new Set();
    let selectedCronId = null;
    const cronMap = new Map();
    const cronMonitorCache = new Map();

    function saveSnapshot() {{
      try {{
        localStorage.setItem(DASH_SNAPSHOT_KEY, JSON.stringify({{
          ts: Date.now(),
          data: dashboardState,
        }}));
      }} catch (e) {{}}
    }}

    function loadSnapshot() {{
      try {{
        const raw = localStorage.getItem(DASH_SNAPSHOT_KEY);
        if (!raw) return false;
        const obj = JSON.parse(raw);
        if (!obj || !obj.data) return false;
        Object.assign(dashboardState, obj.data);
        dashboardState.generatedAt = Date.now();
        return true;
      }} catch (e) {{
        return false;
      }}
    }}

    function keysForPage(page) {{
      if (page === 'overview') return ['overview', 'openclaw', 'agents'];
      if (page === 'crons') return ['crons'];
      if (page === 'usage') return ['models'];
      if (page === 'memory') return ['memory'];
      return [];
    }}

    async function refreshOne(key, force = false) {{
      const conf = refreshState[key];
      if (!conf) return;
      if (refreshingKeys.has(key)) return;
      if (!force && conf.remain > 0) return;

      const headers = {{}};
      if (token()) headers['Authorization'] = 'Bearer ' + token();

      refreshingKeys.add(key);
      try {{
        const data = await fetchJson(conf.url, headers);
        dashboardState[key] = data;

        if (data && typeof data === 'object') {{
          if (data._sourceLagsMs && typeof data._sourceLagsMs === 'object') {{
            dashboardState.sourceLagsMs = data._sourceLagsMs;
          }}
          if (data._sourceTimestamps && typeof data._sourceTimestamps === 'object') {{
            dashboardState.sourceTimestamps = data._sourceTimestamps;
          }}
        }}

        dashboardState.generatedAt = Date.now();
        conf.remain = conf.interval;
        render(dashboardState);
        saveSnapshot();
      }} catch (e) {{
        console.warn(`refresh failed: ${{key}}`, e);
      }} finally {{
        refreshingKeys.delete(key);
      }}
    }}

    function refreshForPage(page, force = false) {{
      const keys = keysForPage(page);
      keys.forEach((key, idx) => setTimeout(() => refreshOne(key, force), idx * 120));
    }}

    function normalizePage(page) {{
      const p = String(page || '').trim().toLowerCase();
      return VALID_PAGES.has(p) ? p : 'overview';
    }}

    function pageFromUrl() {{
      try {{
        const u = new URL(window.location.href);
        const qp = normalizePage(u.searchParams.get('page'));
        if (qp !== 'overview') return qp;
        const hp = normalizePage((u.hash || '').replace(/^#/, ''));
        return hp;
      }} catch (e) {{
        return 'overview';
      }}
    }}

    function syncUrlForPage(page, {{ replace = true }} = {{}}) {{
      const p = normalizePage(page);
      try {{
        const u = new URL(window.location.href);
        const curr = normalizePage(u.searchParams.get('page'));
        if (curr === p && !u.hash) return;
        u.searchParams.set('page', p);
        u.hash = '';
        const next = `${{u.pathname}}?${{u.searchParams.toString()}}`;
        if (replace) history.replaceState({{ page: p }}, '', next);
        else history.pushState({{ page: p }}, '', next);
      }} catch (e) {{}}
    }}

    function setActivePage(page, {{ forceRefresh = false, replaceUrl = true }} = {{}}) {{
      const p = normalizePage(page);
      activePage = p;

      document.querySelectorAll('.nav-tab').forEach(x => {{
        const xp = normalizePage(x.getAttribute('data-page'));
        x.classList.toggle('active', xp === p);
      }});

      document.querySelectorAll('.page').forEach(x => x.classList.remove('active'));
      const pageEl = document.getElementById('page-' + p);
      if (pageEl) pageEl.classList.add('active');

      syncUrlForPage(p, {{ replace: replaceUrl }});
      refreshForPage(p, forceRefresh);
    }}

    function bootstrapRefresh() {{
      const hasSnap = loadSnapshot();
      if (hasSnap) render(dashboardState);

      const initialPage = pageFromUrl();
      setActivePage(initialPage, {{ forceRefresh: true, replaceUrl: true }});

      // 其他页数据按需加载（切换标签时触发）
    }}

    function render(d) {{

      const ov = d.overview || {{}};
      const o = d.openclaw || {{}};
      const cards = [
        ['OpenClaw', o.state || '-', '状态自动刷新'],
        ['Agents', ov.agentCount, `运行中 ${{fmtNum(ov.runningAgents)}}`],
        ['Sub-Agents', ov.subagentCount, `运行中 ${{fmtNum(ov.runningSubagents)}}`],
        ['Crons', ov.cronCount, `运行中 ${{fmtNum(ov.runningCrons)}}`],
        ['Models', ov.modelCount, `总 Tokens ${{fmtNum(ov.tokens)}}`],
      ];
      $('#cards').innerHTML = cards.map(([k,v,s]) => `
        <div class="card">
          <div class="k">${{k}}</div>
          <div class="v ${{typeof v === 'string' ? 'sm' : ''}}">${{typeof v === 'number' ? fmtNum(v) : v}}</div>
          <div class="meta">${{s}}</div>
        </div>
      `).join('');

      const agents = (d.agents || {{}}).agents || [];
      $('#agents-body').innerHTML = agents.map(a => `
        <tr>
          <td>${{a.id}}</td>
          <td>${{stateBadge(a.running)}} ${{a.running ? 'running' : 'idle'}}</td>
          <td>${{a.subagentsRunning}} / ${{a.subagentsTotal}}</td>
          <td>${{fmtNum(a.sessionsCount)}}</td>
          <td>${{a.heartbeatEnabled ? 'on (' + (a.heartbeatEvery || '-') + ')' : 'off'}}</td>
        </tr>
      `).join('') || '<tr><td colspan="5" class="meta">暂无数据</td></tr>';


      const crons = (d.crons || {{}}).jobs || [];
      cronMap.clear();
      crons.forEach(c => cronMap.set(c.id, c));

      const cronsBody = $('#crons-body');
      cronsBody.innerHTML = crons.map(c => `
        <tr>
          <td>${{c.name}}</td>
          <td>${{c.agentId}}</td>
          <td>${{c.enabled ? '<span class="ok">yes</span>' : '<span class="warn">no</span>'}}</td>
          <td>${{c.running ? '<span class="ok">running</span>' : '-'}}</td>
          <td>${{c.lastStatus || '-'}}</td>
          <td>${{c.lastDurationMs ? Math.round(c.lastDurationMs/1000)+'s' : '-'}}</td>
          <td>${{fmtTime(c.nextRunAtMs)}}</td>
          <td><button class="btn-monitor" data-cron-id="${{c.id}}">监控</button></td>
        </tr>
      `).join('') || '<tr><td colspan="8" class="meta">暂无数据</td></tr>';

      cronsBody.querySelectorAll('.btn-monitor').forEach(btn => {{
        btn.addEventListener('click', () => showCronMonitor(btn.getAttribute('data-cron-id')));
      }});

      if (!selectedCronId && crons.length > 0) {{
        selectedCronId = crons[0].id;
      }}
      renderCronMonitorList(crons);
      showCronMonitor(selectedCronId);

      const daily = (d.models || {{}}).dailyTokens || [];
      renderDailyChart(daily);

      const modelsData = d.models || {{}};
      const models = (modelsData.models || []).filter(m => Number(m.tokens || 0) > 0);
      const metaEl = $('#token-consumption-meta');
      if (metaEl) {{
        metaEl.textContent = `${{modelsData.formula || '实际消耗 Token = 净输入 + 输出；净输入 = max(0, 输入 - 缓存复用)'}}｜主动 ${{fmtNum(modelsData.activeTokens || 0)}}（${{fmtNum(modelsData.activeSessions || 0)}} sessions）｜被动 ${{fmtNum(modelsData.passiveTokens || 0)}}（${{fmtNum(modelsData.passiveSessions || 0)}} sessions）`;
      }}

      $('#models-body').innerHTML = models.map(m => `
        <tr>
          <td>${{m.model}}</td>
          <td>${{m.provider || '-'}}</td>
          <td>${{fmtNum(m.sessions)}}</td>
          <td>${{fmtNum(m.tokens)}}</td>
          <td>${{fmtNum(m.activeTokens || 0)}}</td>
          <td>${{fmtNum(m.passiveTokens || 0)}}</td>
          <td>${{fmtNum(m.inputTokens)}} <span class="meta">/ ${{fmtNum(m.rawInputTokens || m.inputTokens)}}</span></td>
          <td>${{fmtNum(m.outputTokens)}}</td>
          <td>${{fmtNum(m.cacheRead)}} / ${{fmtNum(m.cacheWrite)}}</td>
        </tr>
      `).join('') || '<tr><td colspan="9" class="meta">暂无数据</td></tr>';

      renderMemory(d.memory || {{}});
    }}

    function renderCronMonitorList(crons) {{
      const listEl = $('#cron-monitor-list');
      const detailTitle = $('#cron-monitor-detail-title');
      const detailEl = $('#cron-monitor-detail');
      if (!listEl) return;
      if (!crons || !crons.length) {{
        listEl.innerHTML = '<div class="meta">暂无任务</div>';
        if (detailTitle) detailTitle.textContent = '执行内容';
        if (detailEl) detailEl.textContent = '当前没有可监督的任务。';
        return;
      }}
      listEl.innerHTML = (crons || []).map(c => `
        <button class="monitor-list-item ${{selectedCronId === c.id ? 'active' : ''}}" data-cron-id="${{c.id}}">
          <div><strong>${{c.name}}</strong></div>
          <div class="meta">${{c.agentId || '-'}} · ${{c.running ? '运行中' : (c.enabled ? '等待中' : '未启用')}}</div>
        </button>
      `).join('') || '<div class="meta">暂无任务</div>';

      listEl.querySelectorAll('.monitor-list-item').forEach(btn => {{
        btn.addEventListener('click', () => showCronMonitor(btn.getAttribute('data-cron-id')));
      }});
    }}

    async function showCronMonitor(id) {{
      const c = id ? cronMap.get(id) : null;
      const detailTitle = $('#cron-monitor-detail-title');
      const detailEl = $('#cron-monitor-detail');
      if (!c || !detailEl) return;
      selectedCronId = id;
      renderCronMonitorList(Array.from(cronMap.values()));

      if (detailTitle) detailTitle.textContent = `执行内容 · ${{c.name}}（${{c.agentId || '-'}}）`;

      if (cronMonitorCache.has(id)) {{
        detailEl.textContent = cronMonitorCache.get(id);
        return;
      }}

      detailEl.textContent = '加载监控内容中...';
      try {{
        const headers = {{}};
        if (token()) headers['Authorization'] = 'Bearer ' + token();
        const data = await fetchJson(`/api/cron-monitor/${{encodeURIComponent(id)}}`, headers);
        const txt = (data && data.monitorText) ? data.monitorText : '该任务未提供具体工作内容。';
        cronMonitorCache.set(id, txt);
        if (selectedCronId === id) detailEl.textContent = txt;
      }} catch (e) {{
        detailEl.textContent = '监控内容加载失败，请稍后重试。';
      }}
    }}

    function fmtBytes(n) {{
      const v = Number(n || 0);
      if (v < 1024) return v + ' B';
      if (v < 1024 * 1024) return (v / 1024).toFixed(1) + ' KB';
      if (v < 1024 * 1024 * 1024) return (v / 1024 / 1024).toFixed(1) + ' MB';
      return (v / 1024 / 1024 / 1024).toFixed(2) + ' GB';
    }}

    function renderMemory(m) {{
      const s = m.summary || {{}};
      const c = m.central || {{}};
      const st = m.storage || {{}};

      const cards = [
        ['工作区', s.workspaceCount || 0, `总条目 ${{fmtNum(s.totalEntries || 0)}}`],
        ['今日新增', s.todayEntries || 0, `最近文件 ${{fmtNum(s.recentCount || 0)}}`],
        ['中央记忆主题', c.topicsCount || 0, `memories-md ${{fmtNum(c.memoriesMdCount || 0)}}`],
        ['向量存储', st.sqliteCount || 0, `SQLite ${{fmtBytes(st.sqliteBytes || 0)}}`],
      ];
      const cardsEl = $('#memory-cards');
      if (cardsEl) {{
        cardsEl.innerHTML = cards.map(([k,v,meta]) => `
          <div class="card">
            <div class="k">${{k}}</div>
            <div class="v">${{fmtNum(v)}}</div>
            <div class="meta">${{meta}}</div>
          </div>
        `).join('');
      }}

      const wsRows = m.workspaces || [];
      $('#memory-workspaces-body').innerHTML = wsRows.map(w => `
        <tr>
          <td>${{w.workspace}}</td>
          <td>${{fmtNum(w.entries)}}</td>
          <td>${{fmtNum(w.todayEntries)}}</td>
          <td>${{w.latestFile || '-'}}</td>
          <td>${{fmtTime(w.latestAt)}}</td>
        </tr>
      `).join('') || '<tr><td colspan="5" class="meta">暂无数据</td></tr>';

      const recRows = m.recent || [];
      $('#memory-recent-body').innerHTML = recRows.map(r => `
        <tr>
          <td>${{r.workspace}}</td>
          <td>${{r.file}}</td>
          <td>${{fmtBytes(r.size)}}</td>
          <td>${{fmtTime(r.updatedAt)}}</td>
        </tr>
      `).join('') || '<tr><td colspan="4" class="meta">暂无数据</td></tr>';

      const p = m.plugin || {{}};
      const pDescEl = $('#memory-plugin-desc');
      if (pDescEl) {{
        const lines = [];
        lines.push(`插件: ${{p.id || '-'}}（别名: ${{p.alias || '-'}}）`);
        lines.push(`状态: ${{p.enabled ? 'enabled' : 'disabled'}}`);
        (p.description || []).forEach(x => lines.push(`- ${{x}}`));
        pDescEl.textContent = lines.join('\\n');
      }}

      const impacts = m.impacts || {{}};
      const impEl = $('#memory-impact');
      if (impEl) {{
        const si = impacts.selfImprove || {{}};
        const iv = impacts.involve || {{}};
        const lines = [];

        lines.push(`self-improve 影响任务数: ${{fmtNum(si.count || 0)}}`);
        (si.items || []).forEach((it, idx) => {{
          lines.push(`  ${{idx + 1}}. ${{it.name}} | ${{it.enabled ? 'enabled' : 'disabled'}} | last=${{it.lastStatus || '-'}} | ${{it.effect || ''}}`);
        }});

        lines.push('');
        lines.push(`involve 影响任务数: ${{fmtNum(iv.count || 0)}}`);
        (iv.items || []).forEach((it, idx) => {{
          lines.push(`  ${{idx + 1}}. ${{it.name}} | ${{it.enabled ? 'enabled' : 'disabled'}} | last=${{it.lastStatus || '-'}} | ${{it.effect || ''}}`);
        }});

        impEl.textContent = lines.join('\\n');
      }}
    }}

    function renderDailyChart(rows) {{
      const el = $('#daily-token-chart');
      const metaEl = $('#daily-token-meta');
      if (!el) return;
      if (!rows || !rows.length) {{
        if (metaEl) metaEl.textContent = '暂无数据';
        el.className = 'meta';
        el.textContent = '暂无数据';
        return;
      }}

      const recentRows = rows.slice(-15);
      const maxVal = Math.max(1, ...recentRows.map(r => Number(r.tokens || 0)));
      const html = recentRows.map((r) => {{
        const total = Number(r.tokens || 0);
        const active = Number(r.activeTokens || 0);
        const passive = Number(r.passiveTokens || 0);
        const h = Math.max(12, Math.round((total / maxVal) * 150));

        let activeH = 0;
        let passiveH = 0;
        if (total > 0) {{
          if (active <= 0) {{
            activeH = 0;
            passiveH = h;
          }} else if (passive <= 0) {{
            passiveH = 0;
            activeH = h;
          }} else {{
            activeH = Math.round((active / total) * h);
            activeH = Math.max(2, Math.min(h - 3, activeH));
            passiveH = h - activeH;
            if (passiveH < 3) {{
              passiveH = 3;
              activeH = Math.max(0, h - 3);
            }}
          }}
        }}

        if (passive > 0 && total > 0 && passiveH < 3) {{
          const delta = 3 - passiveH;
          passiveH = 3;
          activeH = Math.max(0, activeH - delta);
        }}

        const label = String(r.date || '').slice(5);
        const title = `${{r.date}}: 总 ${{fmtNum(total)}}（主动 ${{fmtNum(active)}} / 被动 ${{fmtNum(passive)}}）`;
        const passiveCls = passive > 0 ? 'daily-bar-passive has-value' : 'daily-bar-passive';
        return `
          <div class="daily-item" title="${{title}}">
            <div class="daily-value">${{fmtM(total)}}</div>
            <div class="daily-bar-wrap">
              <div class="daily-bar-stack" style="height:${{h}}px">
                <div class="${{passiveCls}}" style="height:${{passiveH}}px"></div>
                <div class="daily-bar-active" style="height:${{activeH}}px"></div>
              </div>
            </div>
            <div class="daily-label">${{label}}</div>
          </div>
        `;
      }}).join('');

      const today = recentRows[recentRows.length - 1] || {{}};
      if (metaEl) {{
        metaEl.textContent = `最近15天｜今日总消耗: ${{fmtM(today.tokens || 0)}}（主动 ${{fmtNum(today.activeTokens || 0)}} / 被动 ${{fmtNum(today.passiveTokens || 0)}}）`;
      }}

      el.className = 'daily-chart';
      el.innerHTML = html;
    }}

    document.querySelectorAll('.nav-tab').forEach(btn => {{
      btn.addEventListener('click', () => {{
        const page = btn.getAttribute('data-page');
        setActivePage(page, {{ forceRefresh: true, replaceUrl: true }});
      }});
    }});

    window.addEventListener('popstate', () => {{
      const page = pageFromUrl();
      setActivePage(page, {{ forceRefresh: true, replaceUrl: true }});
    }});

    function paintCountdown() {{
      const map = {{
        '#cd-agents': (refreshState.agents && refreshState.agents.remain != null) ? refreshState.agents.remain : 0,
        '#cd-crons': (refreshState.crons && refreshState.crons.remain != null) ? refreshState.crons.remain : 0,
        '#cd-tokens': (refreshState.models && refreshState.models.remain != null) ? refreshState.models.remain : 0,
        '#cd-memory': (refreshState.memory && refreshState.memory.remain != null) ? refreshState.memory.remain : 0,
      }};
      Object.entries(map).forEach(([sel, sec]) => {{
        const el = $(sel);
        if (el) el.textContent = `${{sec}}秒后刷新`;
      }});
    }}

    paintCountdown();
    bootstrapRefresh();

    setInterval(() => {{
      if (document.hidden) return;

      Object.values(refreshState).forEach(conf => {{
        conf.remain -= 1;
        if (conf.remain < 0) conf.remain = 0;
      }});

      const activeKeys = new Set(keysForPage(activePage));
      Object.keys(refreshState).forEach(key => {{
        const conf = refreshState[key];
        if (conf.remain <= 0 && activeKeys.has(key)) {{
          refreshOne(key, true);
        }}
      }});

      paintCountdown();
    }}, 1000);
  </script>
</body>
</html>
"""


def _process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _read_pid() -> Optional[int]:
    try:
        if not PID_FILE.exists():
            return None
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _write_pid(pid: int) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(pid), encoding="utf-8")


def _remove_pid() -> None:
    try:
        if PID_FILE.exists():
            PID_FILE.unlink()
    except Exception:
        pass


def _cmd_status(host: str, port: int) -> int:
    pid = _read_pid()
    running = bool(pid and _process_exists(pid))
    print(f"{APP_TITLE} service")
    print("-" * 42)
    print(f"  URL:         http://{host}:{port}")
    print(f"  PID file:    {PID_FILE}")
    print(f"  Log file:    {LOG_FILE}")
    print(f"  Running:     {'yes' if running else 'no'}")
    if running:
        print(f"  PID:         {pid}")
    return 0


def _cmd_stop() -> int:
    pid = _read_pid()
    if not pid or not _process_exists(pid):
        _remove_pid()
        print("Dashboard is not running.")
        return 0

    try:
        os.kill(pid, signal.SIGTERM)
    except Exception as e:
        print(f"Failed to stop process {pid}: {e}")
        return 1

    for _ in range(20):
        if not _process_exists(pid):
            _remove_pid()
            print(f"Stopped dashboard (pid {pid}).")
            return 0
        time.sleep(0.2)

    try:
        os.kill(pid, signal.SIGKILL)
    except Exception:
        pass
    _remove_pid()
    print(f"Force-stopped dashboard (pid {pid}).")
    return 0


def _cmd_start(host: str, port: int, debug: bool) -> int:
    pid = _read_pid()
    if pid and _process_exists(pid):
        print(f"Dashboard already running (pid {pid}).")
        print(f"URL: http://{host}:{port}")
        return 0

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as logf:
        args = [
            sys.executable,
            os.path.abspath(__file__),
            "serve",
            "--host",
            host,
            "--port",
            str(port),
        ]
        if not debug:
            args.append("--no-debug")

        proc = subprocess.Popen(
            args,
            stdout=logf,
            stderr=logf,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            cwd=os.getcwd(),
            env=os.environ.copy(),
        )
        _write_pid(proc.pid)

    print(f"Started {APP_TITLE} (pid {proc.pid})")
    print(f"URL: http://{host}:{port}")
    return 0


def _run_server(host: str, port: int, debug: bool) -> int:
    app = create_app()
    if waitress_serve and not debug:
        waitress_serve(app, host=host, port=port)
    else:
        app.run(host=host, port=port, debug=debug)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"{APP_TITLE} dashboard")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "start", "stop", "restart", "status"])
    parser.add_argument("--host", default="127.0.0.1", help="bind host")
    parser.add_argument("--port", type=int, default=8900, help="bind port")
    parser.add_argument("--debug", action="store_true", help="enable Flask debug mode")
    parser.add_argument("--no-debug", action="store_true", help="disable debug mode")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.version:
        print(f"{APP_TITLE} v{__version__}")
        return 0

    debug = bool(args.debug and not args.no_debug)

    if args.command == "status":
        return _cmd_status(args.host, args.port)
    if args.command == "stop":
        return _cmd_stop()
    if args.command == "start":
        return _cmd_start(args.host, args.port, debug)
    if args.command == "restart":
        _cmd_stop()
        return _cmd_start(args.host, args.port, debug)

    return _run_server(args.host, args.port, debug)


if __name__ == "__main__":
    raise SystemExit(main())
