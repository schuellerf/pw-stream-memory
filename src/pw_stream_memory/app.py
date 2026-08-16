#!/usr/bin/env python3
"""pw-stream-memory: ncurses editor for PipeWire / WirePlumber stream restore.

Closed streams are listed oldest-first at the top. Active streams stay at
the bottom with an empty end time until they disappear. Closed entries are
saved to JSON and reloaded on the next launch.

Enter opens an editor for volume, sink, restore identity, and debounce.
Native Match-by keys merge into WirePlumber stream-properties after disable /
wait / write / enable. Match-by application.process.binary writes a Lua
sidecar (WirePlumber hook). Debounce (per identity) plays default volume
briefly, mutes, then restores; it re-arms when the PipeWire node goes idle
and runs again.
"""

from __future__ import annotations

import argparse
import curses
import json
import locale
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any


PACTL = shutil.which("pactl") or "pactl"
WPCTL = shutil.which("wpctl") or "wpctl"
PW_CLI = shutil.which("pw-cli")
PW_METADATA = shutil.which("pw-metadata")
WP_STATE_SAVE_TIMEOUT_S = 1.0
WP_STATE_LOCK = threading.Lock()
WP_RESTORE_KEYS = (
    "node.stream.restore-props",
    "node.stream.restore-target",
)
HISTORY_VERSION = 2
SAVE_STEPS = 4
DEBOUNCE_VERSION = 1
OVERRIDES_VERSION = 1
DEFAULT_DEBOUNCE_ON_MS = 500.0
DEFAULT_DEBOUNCE_OFF_S = 30.0
BINARY_PROP = "application.process.binary"
LUA_HOOK_SCRIPT = "pw-stream-memory.lua"
LUA_HOOK_CONF = "99-pw-stream-memory.conf"
LUA_HOOK_CACHE_S = 1.0
LUA_HOOK_META_KEY = "pw-stream-memory.hook"
WP_SIDECAR_STATE_NAME = "pw-stream-memory"
# Native WirePlumber restore keys, in formKey() order.
FORM_KEY_PROPS = (
    "media.role",
    "application.id",
    "application.name",
    "media.name",
    "node.name",
)
PULSE_TO_WP_CHANNEL = {
    "mono": "MONO",
    "front-left": "FL",
    "front-right": "FR",
    "front-center": "FC",
    "lfe": "LFE",
    "rear-left": "RL",
    "rear-right": "RR",
    "side-left": "SL",
    "side-right": "SR",
}

def now_iso_dt() -> datetime:
    return datetime.now().astimezone()


def fmt_iso(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def xdg_data_root() -> Path:
    data = os.environ.get("XDG_DATA_HOME")
    return Path(data) if data else Path.home() / ".local" / "share"


def xdg_config_root() -> Path:
    cfg = os.environ.get("XDG_CONFIG_HOME")
    return Path(cfg) if cfg else Path.home() / ".config"


def xdg_state_root() -> Path:
    state = os.environ.get("XDG_STATE_HOME")
    return Path(state) if state else Path.home() / ".local" / "state"


def _app_state_file(name: str) -> Path:
    """Return a state file under pw-stream-memory, or kde_sound_overrider if that is all that exists."""
    current = xdg_state_root() / "pw-stream-memory" / name
    previous = xdg_state_root() / "kde_sound_overrider" / name
    if not current.is_file() and previous.is_file():
        return previous
    return current


def default_history_path() -> Path:
    return _app_state_file("closed-streams.json")


def default_stream_properties_path() -> Path:
    return xdg_state_root() / "wireplumber" / "stream-properties"


def default_debounce_path() -> Path:
    return _app_state_file("debounce.json")


def default_overrides_path() -> Path:
    return _app_state_file("overrides.json")


def canonical_overrides_path() -> Path:
    return xdg_state_root() / "pw-stream-memory" / "overrides.json"


def wireplumber_sidecar_state_path() -> Path:
    return xdg_state_root() / "wireplumber" / WP_SIDECAR_STATE_NAME


def lua_hook_install_paths() -> tuple[Path, Path]:
    script = xdg_data_root() / "wireplumber" / "scripts" / LUA_HOOK_SCRIPT
    conf = xdg_config_root() / "wireplumber" / "wireplumber.conf.d" / LUA_HOOK_CONF
    return script, conf


def debounce_on_ms() -> float:
    return _env_float(
        "PW_STREAM_MEMORY_DEBOUNCE_ON",
        "SOUND_OVERRIDER_DEBOUNCE_ON",
        default=DEFAULT_DEBOUNCE_ON_MS,
    )


def debounce_off_s() -> float:
    return _env_float(
        "PW_STREAM_MEMORY_DEBOUNCE_OFF",
        "SOUND_OVERRIDER_DEBOUNCE_OFF",
        default=DEFAULT_DEBOUNCE_OFF_S,
    )


def _env_float(*names: str, default: float) -> float:
    for name in names:
        raw = os.environ.get(name)
        if raw is None or not str(raw).strip():
            continue
        try:
            return float(raw)
        except ValueError:
            continue
    return default


def atomic_write_text(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def pactl_json(args: list[str]) -> Any:
    out = subprocess.check_output(
        [PACTL, "--format=json", *args],
        stderr=subprocess.PIPE,
        text=True,
        timeout=3,
    )
    return json.loads(out or "null")


def pulse_ch_to_wp(name: str) -> str:
    return PULSE_TO_WP_CHANNEL.get(name.lower(), name.upper()[:4])


def pulse_percent_to_spa_linear(percent: float) -> float:
    """Pulse/pactl/KDE percent is cubic; SPA channelVolumes are linear."""
    return max(0.0, percent / 100.0) ** 3


def volume_from_pactl(volume: Any) -> tuple[float, list[float], list[str]]:
    if not isinstance(volume, dict) or not volume:
        return 1.0, [1.0], ["MONO"]
    linears: list[float] = []
    channels: list[str] = []
    for ch, info in volume.items():
        channels.append(pulse_ch_to_wp(str(ch)))
        pct = 100.0
        if isinstance(info, dict):
            raw = str(info.get("value_percent") or "100%").strip().rstrip("%")
            try:
                pct = float(raw)
            except ValueError:
                pct = 100.0
        linears.append(max(0.0, pct / 100.0))
    overall = max(linears) if linears else 1.0
    return overall, linears, channels


def stringify_props(props: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, val in props.items():
        if val is None:
            continue
        out[str(key)] = str(val)
    return out


def media_class_key(props: dict[str, str]) -> str:
    mc = props.get("media.class") or "Stream/Output/Audio"
    if mc.startswith("Stream/"):
        return mc[len("Stream/") :]
    return mc


def form_key(props: dict[str, str]) -> tuple[str, str, str] | None:
    klass = media_class_key(props)
    for name in FORM_KEY_PROPS:
        value = props.get(name)
        if value:
            return f"{klass}:{name}:{value}", name, value
    return None


def identity_choices(props: dict[str, str]) -> list[tuple[str, str, str, bool]]:
    default = form_key(props)
    default_name = default[1] if default else None
    klass = media_class_key(props)
    choices: list[tuple[str, str, str, bool]] = []
    for name in FORM_KEY_PROPS:
        value = props.get(name)
        if not value:
            continue
        full = f"{klass}:{name}:{value}"
        choices.append((name, value, full, name == default_name))
    binary = props.get(BINARY_PROP)
    if binary:
        full = f"{klass}:{BINARY_PROP}:{binary}"
        choices.append((BINARY_PROP, binary, full, False))
    return choices


def wp_escape_key(key: str) -> str:
    out: list[str] = []
    for ch in key:
        if ch == "\\":
            out.append("\\\\")
        elif ch == " ":
            out.append("\\s")
        elif ch == "[":
            out.append("\\o")
        elif ch == "]":
            out.append("\\c")
        else:
            out.append(ch)
    return "".join(out)


def wp_unescape_key(key: str) -> str:
    mapping = {"s": " ", "o": "[", "c": "]", "\\": "\\"}
    out: list[str] = []
    i = 0
    while i < len(key):
        if key[i] == "\\" and i + 1 < len(key):
            out.append(mapping.get(key[i + 1], key[i + 1]))
            i += 2
        else:
            out.append(key[i])
            i += 1
    return "".join(out)


def parse_stream_properties(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    result: dict[str, dict[str, Any]] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("[") or "=" not in line:
            continue
        key_esc, val = line.split("=", 1)
        key = wp_unescape_key(key_esc)
        try:
            parsed = json.loads(val)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            result[key] = parsed
    return result


def format_wp_number(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        return float(f"{value:.6f}")
    if isinstance(value, list):
        return [format_wp_number(v) for v in value]
    return value


def dump_stream_properties(entries: dict[str, dict[str, Any]]) -> str:
    lines = ["[stream-properties]"]
    for key, obj in entries.items():
        cleaned = {k: format_wp_number(v) for k, v in obj.items()}
        lines.append(f"{wp_escape_key(key)}={json.dumps(cleaned, ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


def merge_stream_properties(path: Path, key: str, values: dict[str, Any]) -> None:
    entries = parse_stream_properties(path)
    current = dict(entries.get(key) or {})
    for name, val in values.items():
        if val is None:
            current.pop(name, None)
        else:
            current[name] = val
    entries[key] = current
    atomic_write_text(path, dump_stream_properties(entries))


def delete_stream_properties_entry(path: Path, key: str) -> bool:
    entries = parse_stream_properties(path)
    if key not in entries:
        return False
    del entries[key]
    atomic_write_text(path, dump_stream_properties(entries))
    return True


def stream_properties_entry(path: Path, key: str) -> dict[str, Any] | None:
    obj = parse_stream_properties(path).get(key)
    return obj if isinstance(obj, dict) else None


def spa_linear_to_pulse_percent(linear: float) -> float:
    return max(0.0, linear) ** (1.0 / 3.0) * 100.0


def describe_wp_entry(obj: dict[str, Any] | None) -> str:
    if not obj:
        return "none"
    parts: list[str] = []
    cvols = obj.get("channelVolumes")
    if isinstance(cvols, list) and cvols:
        try:
            linear = max(float(x) for x in cvols)
        except (TypeError, ValueError):
            linear = 1.0
        parts.append(f"{spa_linear_to_pulse_percent(linear):.0f}%")
    elif obj.get("volume") is not None:
        try:
            parts.append(f"{spa_linear_to_pulse_percent(float(obj['volume'])):.0f}%")
        except (TypeError, ValueError):
            pass
    if obj.get("mute"):
        parts.append("muted")
    target = obj.get("target")
    if isinstance(target, str) and target.strip():
        parts.append(f"pin {target}")
    else:
        parts.append("no pin")
    return "yes  (" + ", ".join(parts) + ")"


def wpctl_get_bool(key: str) -> bool | None:
    if shutil.which("wpctl") is None:
        return None
    try:
        out = subprocess.check_output(
            [WPCTL, "settings", key],
            stderr=subprocess.PIPE,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    for line in out.splitlines():
        if line.strip().lower().startswith("value:"):
            return line.split(":", 1)[1].strip().lower() in ("true", "1", "yes")
    return None


def wpctl_set_bool(key: str, value: bool) -> None:
    subprocess.check_call(
        [WPCTL, "settings", key, "true" if value else "false"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=3,
    )


def _file_stamp(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def wait_for_wp_state_flush(path: Path, timeout: float = WP_STATE_SAVE_TIMEOUT_S) -> bool:
    """Wait until stream-properties is rewritten, or *timeout* seconds.

    WirePlumber flushes Wp.State on a ~1000ms timer. Watch mtime/size after
    restore has been disabled.
    """
    stamp0 = _file_stamp(path)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.05)
        stamp1 = _file_stamp(path)
        if stamp0 is not None and stamp1 is not None and stamp1 != stamp0:
            time.sleep(0.05)
            return True
        if stamp0 is None and stamp1 is not None:
            time.sleep(0.05)
            return True
    return False


def with_stream_properties_reload(
    path: Path,
    write_fn: Callable[[], None],
    on_progress: Callable[[int, int, str], None] | None = None,
) -> str:
    """Disable WP stream restore, let a pending save finish, write, reload."""

    def progress(step: int, label: str) -> None:
        if on_progress is not None:
            on_progress(step, SAVE_STEPS, label)

    with WP_STATE_LOCK:
        if shutil.which("wpctl") is None:
            progress(1, "disable restore (wpctl missing)")
            progress(2, "wait for sync (skipped)")
            progress(3, "save stream-properties")
            write_fn()
            progress(4, "enable restore (skipped)")
            return "wpctl missing; wrote file without reload"

        progress(1, "disable restore")
        previous = {key: wpctl_get_bool(key) for key in WP_RESTORE_KEYS}
        disabled: list[str] = []
        reenable_failed: list[str] = []
        note = "save incomplete"
        try:
            for key, was_on in previous.items():
                if was_on:
                    wpctl_set_bool(key, False)
                    disabled.append(key)
            progress(2, "wait for sync")
            flushed = wait_for_wp_state_flush(path) if disabled else False
            progress(3, "save stream-properties")
            write_fn()
            if not disabled:
                note = "wrote file without WP reload"
            elif flushed:
                note = "WP flushed then reloaded"
            else:
                note = "WP reload after 1s wait"
        finally:
            progress(4, "enable restore")
            for key in disabled:
                try:
                    wpctl_set_bool(key, True)
                except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    reenable_failed.append(key)
        if reenable_failed:
            note += f"; failed to re-enable {', '.join(reenable_failed)}"
        return note


def stored_restore_target(stream_properties_path: Path, key: str) -> str | None:
    entries = parse_stream_properties(stream_properties_path)
    target = (entries.get(key) or {}).get("target")
    if isinstance(target, str) and target.strip():
        return target
    return None


@dataclass
class LuaHookInfo:
    status: str
    script_path: Path
    conf_path: Path
    wp_pid: int | None


_LUA_HOOK_CACHE: tuple[float, LuaHookInfo] | None = None


def wireplumber_main_pid() -> int | None:
    try:
        out = subprocess.check_output(
            ["systemctl", "--user", "show", "-p", "MainPID", "--value", "wireplumber.service"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
        pid = int((out or "0").strip() or "0")
        if pid > 0:
            return pid
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
        pass
    pgrep = shutil.which("pgrep")
    if not pgrep:
        return None
    try:
        out = subprocess.check_output(
            [pgrep, "-u", str(os.getuid()), "-x", "wireplumber"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    for token in (out or "").split():
        try:
            pid = int(token)
        except ValueError:
            continue
        if pid > 0:
            return pid
    return None


def lua_hook_metadata_loaded() -> bool:
    cmd = PW_METADATA or shutil.which("pw-metadata")
    if not cmd:
        return False
    try:
        out = subprocess.check_output(
            [cmd, "-n", "default"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return LUA_HOOK_META_KEY in (out or "")


def lua_hook_info(*, force: bool = False) -> LuaHookInfo:
    global _LUA_HOOK_CACHE
    now = time.monotonic()
    if not force and _LUA_HOOK_CACHE is not None and now - _LUA_HOOK_CACHE[0] < LUA_HOOK_CACHE_S:
        return _LUA_HOOK_CACHE[1]
    script, conf = lua_hook_install_paths()
    wp_pid = wireplumber_main_pid()
    installed = script.is_file() and conf.is_file()
    running = bool(wp_pid) and lua_hook_metadata_loaded()
    if running:
        status = "running"
    elif installed:
        status = "installed"
    else:
        status = "missing"
    info = LuaHookInfo(
        status=status,
        script_path=script,
        conf_path=conf,
        wp_pid=wp_pid,
    )
    _LUA_HOOK_CACHE = (now, info)
    return info


def lua_hook_messages(info: LuaHookInfo) -> tuple[str, str]:
    if info.status == "running":
        return (
            "Lua hook: running  ·  Restore: Lua sidecar (no stream-properties reload)",
            "Stock WirePlumber still restores Chromium; this overlay applies after that.",
        )
    if info.status == "installed":
        return (
            "Lua hook: installed, not loaded",
            "Restart WirePlumber to load the hook: systemctl --user restart wireplumber",
        )
    return (
        "Lua hook: not installed",
        "Install with pw-stream-memory --install-lua-hook, then restart WirePlumber.",
    )


def load_overrides(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or default_overrides_path()
    if not target.is_file():
        return []
    with target.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        items = data.get("overrides") or []
    elif isinstance(data, list):
        items = data
    else:
        return []
    return [item for item in items if isinstance(item, dict)]


def save_overrides(items: list[dict[str, Any]]) -> None:
    payload = {"version": OVERRIDES_VERSION, "overrides": items}
    atomic_write_text(
        canonical_overrides_path(),
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )
    # JSON for the TUI; Wp.State copy for the Lua hook.
    compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    atomic_write_text(
        wireplumber_sidecar_state_path(),
        f"[{WP_SIDECAR_STATE_NAME}]\noverrides={compact}\n",
    )


def override_media_class(item: dict[str, Any]) -> str | None:
    want = item.get("media_class")
    return want if isinstance(want, str) and want else None


def find_binary_override(
    props: dict[str, str],
    *,
    value: str | None = None,
    overrides: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    binary = value if value is not None else props.get(BINARY_PROP)
    if not binary:
        return None
    klass = media_class_key(props)
    raw = props.get("media.class") or ""
    for item in overrides if overrides is not None else load_overrides():
        if item.get("prop") != BINARY_PROP or item.get("value") != binary:
            continue
        want = override_media_class(item)
        if want and want not in (klass, raw):
            continue
        return item
    return None


def sidecar_restore_values(result: EditorResult, rec: StreamRecord) -> dict[str, Any]:
    spa_linear = pulse_percent_to_spa_linear(result.percent)
    cmap = rec.channel_map or ["FL", "FR"]
    cvols = [spa_linear] * max(1, len(cmap))
    return {
        "mute": result.mute,
        "volume": 1.0,
        "channelVolumes": cvols,
        "channelMap": cmap,
        "target": result.sink_name,
    }


def upsert_binary_override(rec: StreamRecord, result: EditorResult) -> None:
    values = sidecar_restore_values(result, rec)
    klass = media_class_key(rec.properties)
    entry = {
        "prop": BINARY_PROP,
        "value": result.value,
        "media_class": klass,
        **values,
    }
    kept: list[dict[str, Any]] = []
    for item in load_overrides():
        if (
            item.get("prop") == BINARY_PROP
            and item.get("value") == result.value
            and (override_media_class(item) in (None, klass))
        ):
            continue
        kept.append(item)
    kept.append(entry)
    save_overrides(kept)


def delete_binary_override(result: EditorResult) -> bool:
    klass = result.key.split(":", 1)[0] if ":" in result.key else ""
    kept: list[dict[str, Any]] = []
    found = False
    for item in load_overrides():
        if item.get("prop") == result.prop and item.get("value") == result.value:
            want = override_media_class(item)
            if want and klass and want != klass:
                kept.append(item)
                continue
            found = True
            continue
        kept.append(item)
    if not found:
        return False
    save_overrides(kept)
    return True


def stored_sidecar_target(props: dict[str, str], value: str) -> str | None:
    ov = find_binary_override(props, value=value)
    if not ov:
        return None
    target = ov.get("target")
    if isinstance(target, str) and target.strip():
        return target
    return None


def load_debounce_entries(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        items = data.get("entries") or []
    elif isinstance(data, list):
        items = data
    else:
        return []
    return [item for item in items if isinstance(item, dict)]


def upsert_debounce(path: Path, entry: dict[str, Any]) -> None:
    items = load_debounce_entries(path)
    key = entry.get("key")
    kept: list[dict[str, Any]] = []
    for item in items:
        if item.get("key") != key:
            kept.append(item)
    if entry.get("enabled"):
        kept.append(entry)
    payload = {"version": DEBOUNCE_VERSION, "entries": kept}
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def debounce_enabled_for(path: Path, key: str) -> bool:
    for item in load_debounce_entries(path):
        if item.get("key") == key and item.get("enabled"):
            return True
    return False


def debounce_entry_matches(entry: dict[str, Any], props: dict[str, str]) -> bool:
    if not entry.get("enabled"):
        return False
    prop = entry.get("prop")
    value = entry.get("value")
    if not isinstance(prop, str) or not isinstance(value, str) or not prop:
        return False
    return props.get(prop) == value


def pipewire_node_state(object_id: str) -> str | None:
    if not object_id or shutil.which("pw-cli") is None:
        return None
    try:
        out = subprocess.check_output(
            [PW_CLI or "pw-cli", "info", object_id],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("state:"):
            return stripped.split(":", 1)[1].strip().strip('"')
    return None


def record_to_json(rec: StreamRecord) -> dict[str, Any]:
    return {
        "index": rec.index,
        "start": fmt_iso(rec.start),
        "end": fmt_iso(rec.end),
        "label": rec.label,
        "mute": rec.mute,
        "corked": rec.corked,
        "properties": rec.properties,
        "volume_linear": rec.volume_linear,
        "sink_name": rec.sink_name,
        "channel_map": rec.channel_map,
        "channel_volumes": rec.channel_volumes,
    }


def record_from_json(item: dict[str, Any], serial: int) -> StreamRecord | None:
    try:
        end_raw = item.get("end")
        if not end_raw:
            return None
        start = parse_iso(str(item["start"]))
        end = parse_iso(str(end_raw))
        props_raw = item.get("properties") or {}
        props = stringify_props(props_raw) if isinstance(props_raw, dict) else {}
        cmap = item.get("channel_map") or []
        cvol = item.get("channel_volumes") or []
        vol = item.get("volume_linear")
        return StreamRecord(
            index=int(item.get("index", 0)),
            start=start,
            end=end,
            label=str(item.get("label") or "unknown"),
            mute=bool(item.get("mute")),
            corked=bool(item.get("corked")),
            serial=serial,
            properties=props,
            volume_linear=float(vol) if vol is not None else None,
            sink_name=str(item["sink_name"]) if item.get("sink_name") else None,
            channel_map=[str(x) for x in cmap] if isinstance(cmap, list) else [],
            channel_volumes=[float(x) for x in cvol] if isinstance(cvol, list) else [],
        )
    except (KeyError, TypeError, ValueError):
        return None


def load_closed_records(path: Path) -> list[StreamRecord]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("records") or []
    else:
        return []
    records: list[StreamRecord] = []
    for i, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        rec = record_from_json(item, serial=i)
        if rec is not None:
            records.append(rec)
    return records


def save_closed_records(path: Path, records: list[StreamRecord]) -> None:
    closed = [record_to_json(r) for r in records if r.end is not None]
    payload = {"version": HISTORY_VERSION, "records": closed}
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def stream_label(props: dict[str, Any]) -> str:
    app = (
        props.get("application.name")
        or props.get("node.name")
        or props.get("application.process.binary")
        or "unknown"
    )
    media = props.get("media.name") or ""
    role = props.get("media.role") or ""
    binary = props.get("application.process.binary") or ""
    pid = props.get("application.process.id") or ""

    if media and media.casefold() not in {app.casefold(), "playback", "audio stream"}:
        name = f"{app} — {media}"
    else:
        name = app
    extra: list[str] = []
    if role:
        extra.append(role)
    if binary and binary.casefold() not in name.casefold():
        extra.append(binary)
    if pid:
        extra.append(f"pid {pid}")
    if extra:
        name = f"{name}  ({', '.join(extra)})"
    return name


def list_sinks() -> list[tuple[str, str]]:
    try:
        data = pactl_json(["list", "sinks"])
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    sinks: list[tuple[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if not name:
            continue
        desc = str(item.get("description") or name)
        sinks.append((name, desc))
    return sinks


def sinks_by_index() -> dict[int, str]:
    try:
        data = pactl_json(["list", "sinks"])
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return {}
    if not isinstance(data, list):
        return {}
    mapping: dict[int, str] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item["index"])
        except (KeyError, TypeError, ValueError):
            continue
        name = str(item.get("name") or "")
        if name:
            mapping[idx] = name
    return mapping


def live_sink_input_indexes() -> set[int]:
    try:
        data = pactl_json(["list", "sink-inputs"])
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return set()
    if not isinstance(data, list):
        return set()
    indexes: set[int] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            indexes.add(int(item["index"]))
        except (KeyError, TypeError, ValueError):
            continue
    return indexes


def apply_live_stream(index: int, percent: float, mute: bool, sink_name: str | None) -> None:
    pct = int(round(percent))
    subprocess.check_call(
        [PACTL, "set-sink-input-volume", str(index), f"{pct}%"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=3,
    )
    subprocess.check_call(
        [PACTL, "set-sink-input-mute", str(index), "1" if mute else "0"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=3,
    )
    if sink_name:
        subprocess.check_call(
            [PACTL, "move-sink-input", str(index), sink_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=3,
        )


def apply_live_volume_mute(index: int, percent: float, mute: bool) -> bool:
    try:
        apply_live_stream(index, percent, mute, None)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return True


@dataclass
class StreamRecord:
    index: int
    start: datetime
    end: datetime | None
    label: str
    mute: bool = False
    corked: bool = False
    serial: int = 0
    properties: dict[str, str] = field(default_factory=dict)
    volume_linear: float | None = None
    sink_name: str | None = None
    channel_map: list[str] = field(default_factory=list)
    channel_volumes: list[float] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.end is None

    def clone(self) -> StreamRecord:
        return StreamRecord(
            index=self.index,
            start=self.start,
            end=self.end,
            label=self.label,
            mute=self.mute,
            corked=self.corked,
            serial=self.serial,
            properties=dict(self.properties),
            volume_linear=self.volume_linear,
            sink_name=self.sink_name,
            channel_map=list(self.channel_map),
            channel_volumes=list(self.channel_volumes),
        )


@dataclass
class Snapshot:
    records: list[StreamRecord]
    error: str | None = None
    pulse_ok: bool = True


@dataclass
class EditorResult:
    prop: str
    value: str
    key: str
    percent: float
    mute: bool
    sink_name: str | None
    is_wp_default: bool
    debounce: bool
    delete_entry: bool = False


@dataclass
class DebounceJob:
    index: int
    serial: int
    percent: float
    key: str
    object_id: str
    rec: StreamRecord
    cancel: threading.Event = field(default_factory=threading.Event)
    phase: str = "on"
    token: int = 0


class DebounceEngine:
    """Play, mute, restore on new streams; re-arm on idle→running or uncork."""

    def __init__(self, config_path: Path, stream_properties_path: Path) -> None:
        self.config_path = config_path
        self.stream_properties_path = stream_properties_path
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._jobs: dict[int, DebounceJob] = {}
        self._token = 0
        self._entries: list[dict[str, Any]] = []
        self.reload()

    def reload(self) -> None:
        try:
            entries = load_debounce_entries(self.config_path)
        except (OSError, json.JSONDecodeError, ValueError):
            entries = []
        with self._lock:
            self._entries = entries

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            jobs = list(self._jobs.values())
            self._jobs.clear()
        for job in jobs:
            job.cancel.set()

    def phase_for(self, index: int) -> str | None:
        with self._lock:
            job = self._jobs.get(index)
            if job is None:
                return None
            return job.phase

    def enabled_for(self, props: dict[str, str]) -> dict[str, Any] | None:
        with self._lock:
            entries = list(self._entries)
        for entry in entries:
            if debounce_entry_matches(entry, props):
                return entry
        return None

    def on_new(self, rec: StreamRecord) -> None:
        if self._stop.is_set():
            return
        if not rec.properties or self.enabled_for(rec.properties) is None:
            return
        self._start_cycle(rec)

    def on_gone(self, rec: StreamRecord) -> None:
        with self._lock:
            job = self._jobs.pop(rec.index, None)
        if job is None:
            return
        job.cancel.set()
        if job.phase == "off":
            self._repair_wp_mute(job)

    def on_corked(self, rec: StreamRecord, was_corked: bool, now_corked: bool) -> None:
        if not (was_corked and not now_corked):
            return
        with self._lock:
            job = self._jobs.get(rec.index)
            phase = job.phase if job else None
        if phase == "idle":
            self._start_cycle(rec)

    def arm_existing(self, rec: StreamRecord) -> None:
        if self.enabled_for(rec.properties) is None:
            return
        job = self._make_job(rec)
        job.phase = "idle"
        with self._lock:
            old = self._jobs.get(rec.index)
            self._jobs[rec.index] = job
        if old is not None:
            old.cancel.set()
        threading.Thread(
            target=self._idle_watch,
            args=(job,),
            daemon=True,
            name=f"debounce-idle-{rec.index}",
        ).start()

    def _make_job(self, rec: StreamRecord) -> DebounceJob:
        ident = form_key(rec.properties)
        percent = 100.0 if rec.volume_linear is None else max(0.0, min(150.0, rec.volume_linear * 100.0))
        with self._lock:
            self._token += 1
            token = self._token
        return DebounceJob(
            index=rec.index,
            serial=rec.serial,
            percent=percent,
            key=ident[0] if ident else "",
            object_id=str(rec.properties.get("object.id") or ""),
            rec=rec.clone(),
            token=token,
        )

    def _start_cycle(self, rec: StreamRecord) -> None:
        job = self._make_job(rec)
        with self._lock:
            old = self._jobs.get(rec.index)
            self._jobs[rec.index] = job
        if old is not None:
            old.cancel.set()
        threading.Thread(target=self._run_cycle, args=(job,), daemon=True, name=f"debounce-{rec.index}").start()

    def _still_current(self, job: DebounceJob) -> bool:
        if self._stop.is_set() or job.cancel.is_set():
            return False
        with self._lock:
            current = self._jobs.get(job.index)
        return current is not None and current.token == job.token

    def _wait(self, job: DebounceJob, seconds: float) -> bool:
        if seconds <= 0:
            return self._still_current(job)
        return not job.cancel.wait(seconds) and self._still_current(job)

    def _run_cycle(self, job: DebounceJob) -> None:
        job.phase = "on"
        if not apply_live_volume_mute(job.index, job.percent, False):
            return
        if not self._wait(job, debounce_on_ms() / 1000.0):
            return
        job.phase = "off"
        if not apply_live_volume_mute(job.index, job.percent, True):
            return
        if not self._wait(job, debounce_off_s()):
            return
        job.phase = "idle"
        if not apply_live_volume_mute(job.index, job.percent, False):
            return
        self._idle_watch(job)

    def _idle_watch(self, job: DebounceJob) -> None:
        saw_quiet = False
        while self._still_current(job):
            state = pipewire_node_state(job.object_id) if job.object_id else None
            if state is None:
                if not self._wait(job, 0.25):
                    return
                continue
            running = state == "running"
            if not running:
                saw_quiet = True
            elif saw_quiet:
                if self._still_current(job):
                    self._start_cycle(job.rec)
                return
            if not self._wait(job, 0.25):
                return

    def _repair_wp_mute(self, job: DebounceJob) -> None:
        if not job.key:
            return
        try:
            with_stream_properties_reload(
                self.stream_properties_path,
                lambda: merge_stream_properties(
                    self.stream_properties_path,
                    job.key,
                    {"mute": False},
                ),
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError):
            pass


class PulseMonitor:
    """Track sink-input lifetime via pactl list + subscribe."""

    def __init__(self, history_path: Path, debounce: DebounceEngine | None = None) -> None:
        self.history_path = history_path
        self.debounce = debounce
        self._lock = threading.Lock()
        self._records: list[StreamRecord] = []
        self._pulse_error: str | None = None
        self._history_error: str | None = None
        self._stop = threading.Event()
        self._wake: queue.Queue[None] = queue.Queue()
        self._serial = 0
        self._threads: list[threading.Thread] = []
        self._dirty = False
        self._debounce_ready = False

    def start(self) -> None:
        self._load()
        self._sync()
        self._debounce_ready = True
        self._arm_existing_debounce()
        t_sub = threading.Thread(target=self._subscribe_loop, name="pactl-subscribe", daemon=True)
        t_poll = threading.Thread(target=self._poll_loop, name="pactl-poll", daemon=True)
        self._threads = [t_sub, t_poll]
        t_sub.start()
        t_poll.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.put(None)
        if self.debounce is not None:
            self.debounce.stop()
        self._persist()

    def _arm_existing_debounce(self) -> None:
        if self.debounce is None:
            return
        with self._lock:
            recs = [r.clone() for r in self._records if r.end is None]
        for rec in recs:
            self.debounce.arm_existing(rec)

    def snapshot(self) -> Snapshot:
        with self._lock:
            records = [r.clone() for r in self._records]
            error = self._pulse_error or self._history_error
        closed = sorted((r for r in records if not r.active), key=lambda r: (r.end, r.start, r.serial))
        active = sorted((r for r in records if r.active), key=lambda r: (r.start, r.serial))
        return Snapshot(records=closed + active, error=error, pulse_ok=error is None)

    def update_record(self, serial: int, **changes: Any) -> None:
        with self._lock:
            for rec in self._records:
                if rec.serial != serial:
                    continue
                for name, value in changes.items():
                    setattr(rec, name, value)
                if rec.end is not None:
                    self._dirty = True
                break
        self._persist()

    def _load(self) -> None:
        try:
            loaded = load_closed_records(self.history_path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            with self._lock:
                self._history_error = f"history load failed: {exc}"
            return
        with self._lock:
            self._records = loaded
            self._serial = max((r.serial for r in loaded), default=0)
            self._dirty = False

    def _persist(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            records = list(self._records)
            self._dirty = False
        try:
            save_closed_records(self.history_path, records)
            with self._lock:
                if self._history_error and self._history_error.startswith("history save failed"):
                    self._history_error = None
        except OSError as exc:
            with self._lock:
                self._dirty = True
                self._history_error = f"history save failed: {exc}"

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._wake.get(timeout=0.4)
            except queue.Empty:
                pass
            if self._stop.is_set():
                return
            while True:
                try:
                    self._wake.get_nowait()
                except queue.Empty:
                    break
            self._sync()

    def _subscribe_loop(self) -> None:
        while not self._stop.is_set():
            try:
                proc = subprocess.Popen(
                    [PACTL, "subscribe"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
            except OSError as exc:
                with self._lock:
                    self._pulse_error = f"pactl subscribe failed: {exc}"
                if self._stop.wait(2.0):
                    return
                continue
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    if self._stop.is_set():
                        break
                    if "sink-input" in line:
                        self._wake.put(None)
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    proc.kill()
            if not self._stop.is_set():
                time.sleep(0.5)

    def _list_sink_inputs(self) -> list[dict[str, Any]]:
        try:
            data = pactl_json(["list", "sink-inputs"])
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            raise RuntimeError(str(exc)) from exc
        if not isinstance(data, list):
            return []
        return data

    def _sync(self) -> None:
        try:
            items = self._list_sink_inputs()
            error = None
        except RuntimeError as exc:
            with self._lock:
                self._pulse_error = str(exc)
            return

        sink_names = sinks_by_index()
        current: dict[int, dict[str, Any]] = {}
        for item in items:
            try:
                idx = int(item["index"])
            except (KeyError, TypeError, ValueError):
                continue
            current[idx] = item

        seen_at = now_iso_dt()
        new_recs: list[StreamRecord] = []
        gone_recs: list[StreamRecord] = []
        cork_events: list[tuple[StreamRecord, bool, bool]] = []
        with self._lock:
            self._pulse_error = error
            active_by_index = {r.index: r for r in self._records if r.end is None}

            for idx, item in current.items():
                props_raw = item.get("properties") or {}
                if not isinstance(props_raw, dict):
                    props_raw = {}
                props = stringify_props(props_raw)
                label = stream_label(props)
                mute = bool(item.get("mute"))
                corked = bool(item.get("corked"))
                linear, ch_vols, ch_map = volume_from_pactl(item.get("volume"))
                try:
                    sink_idx = int(item["sink"])
                except (KeyError, TypeError, ValueError):
                    sink_idx = -1
                sink_name = sink_names.get(sink_idx)
                rec = active_by_index.get(idx)
                if rec is None:
                    self._serial += 1
                    rec = StreamRecord(
                        index=idx,
                        start=seen_at,
                        end=None,
                        label=label,
                        mute=mute,
                        corked=corked,
                        serial=self._serial,
                        properties=props,
                        volume_linear=linear,
                        sink_name=sink_name,
                        channel_map=ch_map,
                        channel_volumes=ch_vols,
                    )
                    self._records.append(rec)
                    new_recs.append(rec.clone())
                else:
                    old_corked = rec.corked
                    rec.label = label
                    rec.mute = mute
                    rec.corked = corked
                    rec.properties = props
                    rec.volume_linear = linear
                    rec.sink_name = sink_name
                    rec.channel_map = ch_map
                    rec.channel_volumes = ch_vols
                    if old_corked != corked:
                        cork_events.append((rec.clone(), old_corked, corked))

            for rec in self._records:
                if rec.end is None and rec.index not in current:
                    rec.end = seen_at
                    self._dirty = True
                    gone_recs.append(rec.clone())
            persist = self._dirty
        if persist:
            self._persist()
        if self.debounce is not None and self._debounce_ready:
            for rec in new_recs:
                self.debounce.on_new(rec)
            for rec in gone_recs:
                self.debounce.on_gone(rec)
            for rec, was_corked, now_corked in cork_events:
                self.debounce.on_corked(rec, was_corked, now_corked)


def _clip(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return "…"
    return text[: width - 1] + "…"


def _add(win: curses.window, y: int, x: int, text: str, width: int, attr: int = 0) -> None:
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x >= w or width <= 0:
        return
    chunk = _clip(text, min(width, w - x))
    try:
        win.addstr(y, x, chunk, attr)
    except curses.error:
        pass


def _home_path(path: Path) -> str:
    text = str(path)
    home = str(Path.home())
    if text.startswith(home + os.sep):
        return "~" + text[len(home) :]
    return text


def draw_save_progress(
    stdscr: curses.window,
    step: int,
    total: int,
    label: str,
    *,
    header_attr: int,
    meta_attr: int,
) -> None:
    _h, w = stdscr.getmaxyx()
    stdscr.erase()
    bar_w = min(40, max(8, w - 4))
    filled = 0 if total <= 0 else min(bar_w, round(bar_w * step / total))
    bar = "#" * filled + "-" * (bar_w - filled)
    _add(stdscr, 0, 0, " Saving ", w, header_attr)
    _add(stdscr, 2, 1, f"{step}/{total}  {label}", w - 2, meta_attr)
    _add(stdscr, 4, 1, f"[{bar}]", w - 2, meta_attr)
    stdscr.noutrefresh()
    curses.doupdate()


def save_stream_settings(
    rec: StreamRecord,
    result: EditorResult,
    *,
    stream_properties_path: Path,
    debounce_path: Path,
    debounce: DebounceEngine | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> str:
    spa_linear = pulse_percent_to_spa_linear(result.percent)
    cmap = rec.channel_map or ["FL", "FR"]
    cvols = [spa_linear] * max(1, len(cmap))
    values: dict[str, Any] = {
        "mute": result.mute,
        "volume": 1.0,
        "channelVolumes": cvols,
        "channelMap": cmap,
        "target": result.sink_name,
    }
    notes: list[str] = []
    if result.prop == BINARY_PROP:
        if lua_hook_info(force=True).status != "running":
            return "Lua hook is not running; binary identity was not saved"
        upsert_binary_override(rec, result)
        notes.append("saved Lua sidecar")
        notes.append("no stream-properties reload")
    else:
        reload_note = with_stream_properties_reload(
            stream_properties_path,
            lambda: merge_stream_properties(stream_properties_path, result.key, values),
            on_progress=on_progress,
        )
        notes.append("saved stream-properties")
        notes.append(reload_note)
    upsert_debounce(
        debounce_path,
        {
            "key": result.key,
            "prop": result.prop,
            "value": result.value,
            "enabled": result.debounce,
        },
    )
    if result.debounce:
        notes.append("debounce on")
    else:
        notes.append("debounce off")
    if debounce is not None:
        debounce.reload()
        if rec.active and result.debounce:
            debounce.arm_existing(rec)
        elif rec.active and not result.debounce:
            debounce.on_gone(rec)
    if rec.active and rec.index in live_sink_input_indexes():
        apply_live_stream(rec.index, result.percent, result.mute, result.sink_name)
        notes.append("applied live via pactl")
    elif rec.active:
        notes.append("stream gone before live apply")
    return "; ".join(notes)


def delete_stream_settings(
    result: EditorResult,
    *,
    stream_properties_path: Path,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> str:
    if result.prop == BINARY_PROP:
        if delete_binary_override(result):
            return f"deleted Lua sidecar override for {result.key}"
        return "no Lua sidecar override for this key"
    existed = stream_properties_entry(stream_properties_path, result.key) is not None
    if not existed:
        return "no stream-properties entry for this key"
    reload_note = with_stream_properties_reload(
        stream_properties_path,
        lambda: delete_stream_properties_entry(stream_properties_path, result.key),
        on_progress=on_progress,
    )
    return f"deleted WP restore for {result.key}; {reload_note}"


def confirm_delete_restore_entry(
    stdscr: curses.window,
    key: str,
    header_attr: int,
    meta_attr: int,
    error_attr: int,
    *,
    lua_sidecar: bool = False,
) -> bool:
    stdscr.nodelay(False)
    stdscr.timeout(-1)
    title = " Delete Lua sidecar override " if lua_sidecar else " Delete WirePlumber restore "
    intro = (
        "Remove the saved Lua sidecar override for:"
        if lua_sidecar
        else "Remove the saved restore entry for:"
    )
    follow = (
        "The next matching stream will not get the Lua overlay."
        if lua_sidecar
        else "The next matching stream will use WirePlumber defaults."
    )
    while True:
        h, w = stdscr.getmaxyx()
        stdscr.erase()
        _add(stdscr, 0, 0, title, w, header_attr)
        _add(stdscr, 2, 1, intro, w - 2, meta_attr)
        _add(stdscr, 4, 1, key, w - 2, error_attr)
        _add(stdscr, 6, 1, follow, w - 2, meta_attr)
        _add(stdscr, h - 1, 0, " y confirm   n/Esc cancel ", w, meta_attr)
        stdscr.noutrefresh()
        curses.doupdate()
        try:
            ch = stdscr.getch()
        except curses.error:
            ch = -1
        if ch in (ord("y"), ord("Y")):
            return True
        if ch in (ord("n"), ord("N"), 27):
            return False


def run_editor(
    stdscr: curses.window,
    rec: StreamRecord,
    *,
    header_attr: int,
    meta_attr: int,
    error_attr: int,
    field_attr: int,
    stream_properties_path: Path,
    debounce_path: Path,
) -> EditorResult | None:
    h, w = stdscr.getmaxyx()
    if not rec.properties:
        stdscr.erase()
        _add(stdscr, 0, 0, " No stream metadata ", w, header_attr)
        _add(
            stdscr,
            2,
            1,
            "This row was saved before properties were stored. Play it once more while this tool is running.",
            w - 2,
            meta_attr,
        )
        _add(stdscr, h - 1, 0, " Esc/Enter back ", w, meta_attr)
        stdscr.noutrefresh()
        curses.doupdate()
        stdscr.timeout(-1)
        stdscr.nodelay(False)
        stdscr.getch()
        stdscr.nodelay(True)
        stdscr.timeout(120)
        return None

    choices = identity_choices(rec.properties)
    if not choices:
        stdscr.erase()
        _add(stdscr, 0, 0, " No identity properties ", w, header_attr)
        _add(stdscr, 2, 1, "WirePlumber has nothing to key this stream by.", w - 2, meta_attr)
        _add(stdscr, h - 1, 0, " Esc/Enter back ", w, meta_attr)
        stdscr.noutrefresh()
        curses.doupdate()
        stdscr.timeout(-1)
        stdscr.nodelay(False)
        stdscr.getch()
        stdscr.nodelay(True)
        stdscr.timeout(120)
        return None

    sinks = list_sinks()
    sink_names: list[str | None] = [None, *[name for name, _desc in sinks]]
    ident_idx = next((i for i, item in enumerate(choices) if item[3]), 0)
    sidecar = find_binary_override(rec.properties)
    if sidecar:
        wanted = str(sidecar.get("value") or "")
        for i, (name, value, _full, _default) in enumerate(choices):
            if name == BINARY_PROP and value == wanted:
                ident_idx = i
                break
    percent = 100.0 if rec.volume_linear is None else max(0.0, min(150.0, rec.volume_linear * 100.0))
    mute = rec.mute

    def sink_idx_for_identity(ident_i: int) -> int:
        prop_i, value_i, key_i, _default = choices[ident_i]
        if prop_i == BINARY_PROP:
            target = stored_sidecar_target(rec.properties, value_i)
        else:
            target = stored_restore_target(stream_properties_path, key_i)
        if not target:
            return 0
        if target not in sink_names:
            sink_names.append(target)
        return sink_names.index(target)

    sink_idx = sink_idx_for_identity(ident_idx)
    sink_touched = False
    debounce = debounce_enabled_for(debounce_path, choices[ident_idx][2])
    debounce_touched = False
    field_i = 0
    field_count = 5
    percent_buf = ""
    message = ""

    stdscr.nodelay(True)
    stdscr.timeout(80)
    while True:
        h, w = stdscr.getmaxyx()
        prop, value, full_key, is_default = choices[ident_idx]
        sink_name = sink_names[sink_idx]
        sink_label = "default (no pin)"
        if sink_name:
            desc = next((d for n, d in sinks if n == sink_name), sink_name)
            sink_label = f"{desc}  ({sink_name})"
        if percent_buf:
            try:
                shown_pct = float(percent_buf)
            except ValueError:
                shown_pct = percent
        else:
            shown_pct = percent

        stdscr.erase()
        _add(stdscr, 0, 0, " Edit stream   Tab/↑↓ field   ←→ change   Enter save   d delete   Esc cancel ", w, header_attr)
        _add(stdscr, 1, 1, rec.label, w - 2, meta_attr)

        if prop == BINARY_PROP:
            match_tag = "  [Lua sidecar]"
        elif is_default:
            match_tag = "  [WP default]"
        else:
            match_tag = "  [custom]"
        rows = [
            ("Match by", f"{prop}={value}" + match_tag),
            ("Volume", f"{shown_pct:.0f}%"),
            ("Sink", sink_label),
            ("Mute", "yes" if mute else "no"),
            ("Debounce", "on" if debounce else "off"),
        ]
        for i, (name, val) in enumerate(rows):
            attr = field_attr if i == field_i else curses.A_NORMAL
            _add(stdscr, 3 + i, 1, f"{'>' if i == field_i else ' '} {name:<10} {val}", w - 2, attr)

        on_ms = debounce_on_ms()
        off_s = debounce_off_s()
        db_help = (
            f"Debounce: {on_ms:.0f}ms at default volume, then mute {off_s:.0f}s, then restore. "
            "Re-arms on idle→running or uncork. PW_STREAM_MEMORY_DEBOUNCE_ON / _OFF override timings."
        )
        if prop == BINARY_PROP:
            lua_info = lua_hook_info()
            status_line, hint_line = lua_hook_messages(lua_info)
            sidecar_obj = find_binary_override(rec.properties, value=value)
            restore_line = f"Sidecar restore: {describe_wp_entry(sidecar_obj)}"
            _add(stdscr, 8, 1, f"Lua key: {full_key}", w - 2, meta_attr)
            _add(stdscr, 9, 1, restore_line, w - 2, meta_attr)
            _add(stdscr, 10, 1, status_line, w - 2, meta_attr if lua_info.status == "running" else error_attr)
            _add(stdscr, 11, 1, hint_line, w - 2, error_attr if lua_info.status != "running" else meta_attr)
            _add(stdscr, 12, 1, db_help, w - 2, meta_attr)
            footer = " ←→ identity/volume/sink   space mute/debounce   d/Del delete sidecar   Enter save "
        else:
            wp_obj = stream_properties_entry(stream_properties_path, full_key)
            wp_line = f"WP restore: {describe_wp_entry(wp_obj)}"
            _add(stdscr, 8, 1, f"WP key: {full_key}", w - 2, meta_attr)
            _add(stdscr, 9, 1, wp_line, w - 2, meta_attr)
            if is_default:
                warn = "Stock WirePlumber restores this key. Live streams are also applied with pactl now."
            else:
                warn = "Custom key is not native: WirePlumber restores only its default identity."
            _add(stdscr, 10, 1, warn, w - 2, error_attr if not is_default else meta_attr)
            _add(stdscr, 11, 1, db_help, w - 2, meta_attr)
            footer = " ←→ identity/volume/sink   space mute/debounce   d/Del delete WP entry   Enter save "
        if message:
            _add(stdscr, 13, 1, message, w - 2, error_attr)
        _add(stdscr, h - 1, 0, footer, w, meta_attr)
        stdscr.noutrefresh()
        curses.doupdate()

        try:
            key = stdscr.getch()
        except curses.error:
            key = -1
        if key in (-1, curses.ERR):
            continue
        if key in (27,):
            return None
        if key in (9, curses.KEY_DOWN, ord("j")):
            percent_buf = ""
            field_i = (field_i + 1) % field_count
        elif key in (curses.KEY_UP, ord("k")) or key == getattr(curses, "KEY_BTAB", -2):
            percent_buf = ""
            field_i = (field_i - 1) % field_count
        elif key in (curses.KEY_LEFT, ord("h")):
            if field_i == 0:
                ident_idx = (ident_idx - 1) % len(choices)
                if not sink_touched:
                    sink_idx = sink_idx_for_identity(ident_idx)
                if not debounce_touched:
                    debounce = debounce_enabled_for(debounce_path, choices[ident_idx][2])
            elif field_i == 1:
                percent_buf = ""
                percent = max(0.0, percent - 5.0)
            elif field_i == 2:
                sink_idx = (sink_idx - 1) % len(sink_names)
                sink_touched = True
            elif field_i == 3:
                mute = not mute
            elif field_i == 4:
                debounce = not debounce
                debounce_touched = True
        elif key in (curses.KEY_RIGHT, ord("l")):
            if field_i == 0:
                ident_idx = (ident_idx + 1) % len(choices)
                if not sink_touched:
                    sink_idx = sink_idx_for_identity(ident_idx)
                if not debounce_touched:
                    debounce = debounce_enabled_for(debounce_path, choices[ident_idx][2])
            elif field_i == 1:
                percent_buf = ""
                percent = min(150.0, percent + 5.0)
            elif field_i == 2:
                sink_idx = (sink_idx + 1) % len(sink_names)
                sink_touched = True
            elif field_i == 3:
                mute = not mute
            elif field_i == 4:
                debounce = not debounce
                debounce_touched = True
        elif key in (ord(" "),):
            if field_i == 3:
                mute = not mute
            elif field_i == 4:
                debounce = not debounce
                debounce_touched = True
        elif key in (ord("d"), ord("D"), curses.KEY_DC):
            if prop == BINARY_PROP:
                if find_binary_override(rec.properties, value=value) is None:
                    message = "No Lua sidecar override for this key"
                    continue
                if not confirm_delete_restore_entry(
                    stdscr, full_key, header_attr, meta_attr, error_attr, lua_sidecar=True
                ):
                    stdscr.nodelay(True)
                    stdscr.timeout(80)
                    continue
            else:
                if stream_properties_entry(stream_properties_path, full_key) is None:
                    message = "No WirePlumber restore entry for this key"
                    continue
                if not confirm_delete_restore_entry(stdscr, full_key, header_attr, meta_attr, error_attr):
                    stdscr.nodelay(True)
                    stdscr.timeout(80)
                    continue
            return EditorResult(
                prop=prop,
                value=value,
                key=full_key,
                percent=percent,
                mute=mute,
                sink_name=sink_name,
                is_wp_default=is_default,
                debounce=debounce,
                delete_entry=True,
            )
        elif field_i == 1 and (curses.KEY_BACKSPACE == key or key in (127, 8)):
            percent_buf = percent_buf[:-1]
            if percent_buf:
                try:
                    percent = max(0.0, min(150.0, float(percent_buf)))
                except ValueError:
                    pass
        elif field_i == 1 and (ord("0") <= key <= ord("9") or key == ord(".")):
            percent_buf += chr(key)
            try:
                typed = float(percent_buf)
                if typed <= 150.0:
                    percent = typed
            except ValueError:
                percent_buf = percent_buf[:-1]
        elif key in (10, 13, curses.KEY_ENTER):
            if prop == BINARY_PROP:
                lua_info = lua_hook_info(force=True)
                if lua_info.status != "running":
                    if lua_info.status == "installed":
                        message = (
                            "Lua hook is not loaded; save refused. "
                            "Restart WirePlumber (systemctl --user restart wireplumber), then save again."
                        )
                    else:
                        message = (
                            "Lua hook is not installed; save refused. "
                            "Run pw-stream-memory --install-lua-hook, then restart WirePlumber."
                        )
                    continue
            return EditorResult(
                prop=prop,
                value=value,
                key=full_key,
                percent=percent,
                mute=mute,
                sink_name=sink_name,
                is_wp_default=is_default,
                debounce=debounce,
            )


class Tui:
    def __init__(
        self,
        monitor: PulseMonitor,
        *,
        stream_properties_path: Path,
        debounce_path: Path,
    ) -> None:
        self.monitor = monitor
        self.stream_properties_path = stream_properties_path
        self.debounce_path = debounce_path
        self.follow = True
        self.scroll = 0
        self.selected_serial: int | None = None
        self.status = ""

    def run(self, stdscr: curses.window) -> None:
        curses.curs_set(0)
        curses.use_default_colors()
        stdscr.nodelay(True)
        stdscr.timeout(120)
        curses.set_escdelay(25)

        if curses.has_colors():
            curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)
            curses.init_pair(2, curses.COLOR_GREEN, -1)
            curses.init_pair(3, curses.COLOR_WHITE, -1)
            curses.init_pair(4, curses.COLOR_RED, -1)
            curses.init_pair(5, curses.COLOR_YELLOW, -1)
            curses.init_pair(6, curses.COLOR_BLACK, curses.COLOR_WHITE)
            header_attr = curses.color_pair(1) | curses.A_BOLD
            active_attr = curses.color_pair(2) | curses.A_BOLD
            closed_attr = curses.color_pair(3) | curses.A_DIM
            error_attr = curses.color_pair(4) | curses.A_BOLD
            meta_attr = curses.color_pair(5)
            sel_attr = curses.color_pair(6) | curses.A_BOLD
        else:
            header_attr = curses.A_REVERSE | curses.A_BOLD
            active_attr = curses.A_BOLD
            closed_attr = curses.A_DIM
            error_attr = curses.A_BOLD
            meta_attr = curses.A_NORMAL
            sel_attr = curses.A_REVERSE

        start_w = 25
        end_w = 25

        while True:
            snap = self.monitor.snapshot()
            rows = snap.records
            h, w = stdscr.getmaxyx()
            body_top = 2
            footer_y = h - 1
            body_h = max(0, footer_y - body_top)
            n = len(rows)
            serials = [r.serial for r in rows]

            if self.follow and rows:
                self.selected_serial = rows[-1].serial
            elif self.selected_serial not in serials:
                self.selected_serial = serials[-1] if serials else None

            selected_idx = serials.index(self.selected_serial) if self.selected_serial in serials else 0
            max_scroll = max(0, n - body_h)
            if selected_idx < self.scroll:
                self.scroll = selected_idx
            elif body_h and selected_idx >= self.scroll + body_h:
                self.scroll = selected_idx - body_h + 1
            self.scroll = max(0, min(self.scroll, max_scroll))

            stdscr.erase()
            title = (
                f" pw-stream-memory  debounce {debounce_on_ms():.0f}ms/{debounce_off_s():.0f}s"
                "   q quit   ↑↓ select   Enter edit   PgUp/PgDn  g/G "
            )
            _add(stdscr, 0, 0, title.ljust(max(w, 1)), w, header_attr)

            gap = 2
            name_x = start_w + gap + end_w + gap
            name_w = max(8, w - name_x)
            _add(stdscr, 1, 0, "START".ljust(start_w), start_w, curses.A_UNDERLINE)
            _add(stdscr, 1, start_w + gap, "END".ljust(end_w), end_w, curses.A_UNDERLINE)
            _add(stdscr, 1, name_x, "STREAM", name_w, curses.A_UNDERLINE)

            visible = rows[self.scroll : self.scroll + body_h]
            for i, rec in enumerate(visible):
                y = body_top + i
                selected = rec.serial == self.selected_serial
                attr = sel_attr if selected else (active_attr if rec.active else closed_attr)
                start_s = fmt_iso(rec.start)
                end_s = fmt_iso(rec.end)
                flags = []
                if rec.mute:
                    flags.append("muted")
                if rec.corked:
                    flags.append("corked")
                if self.monitor.debounce is not None:
                    phase = self.monitor.debounce.phase_for(rec.index) if rec.active else None
                    if phase == "on":
                        flags.append("debounce")
                    elif phase == "off":
                        flags.append("debounce-mute")
                    elif phase == "idle":
                        flags.append("debounce-idle")
                label = rec.label
                if flags:
                    label = f"{label}  [{', '.join(flags)}]"
                marker = ">" if selected else " "
                _add(stdscr, y, 0, start_s, start_w, attr)
                _add(stdscr, y, start_w + gap, end_s, end_w, attr)
                _add(stdscr, y, name_x, f"{marker} {label}", name_w, attr)

            n_active = sum(1 for r in rows if r.active)
            n_closed = n - n_active
            follow_s = "follow" if self.follow else f"row {selected_idx + 1}/{n or 0}"
            hist = _home_path(self.monitor.history_path)
            footer = f" {n_closed} closed  ·  {n_active} active  ·  {follow_s}  ·  {hist} "
            if self.status:
                footer = f" {self.status}  · {footer}"
            if snap.error:
                footer = f" {snap.error}  · {footer}"
                fattr = error_attr
            else:
                fattr = meta_attr
            _add(stdscr, footer_y, 0, footer.ljust(max(w, 1)), w, fattr)

            stdscr.noutrefresh()
            curses.doupdate()

            try:
                key = stdscr.getch()
            except curses.error:
                key = -1
            if key in (-1, curses.ERR):
                continue
            if key in (ord("q"), ord("Q")):
                return
            if key in (curses.KEY_UP, ord("k")):
                self.follow = False
                if selected_idx > 0:
                    self.selected_serial = serials[selected_idx - 1]
            elif key in (curses.KEY_DOWN, ord("j")):
                if selected_idx < n - 1:
                    self.follow = False
                    self.selected_serial = serials[selected_idx + 1]
                elif n:
                    self.follow = True
            elif key in (curses.KEY_PPAGE,):
                self.follow = False
                new_idx = max(0, selected_idx - max(1, body_h - 1))
                if serials:
                    self.selected_serial = serials[new_idx]
            elif key in (curses.KEY_NPAGE,):
                new_idx = min(max(0, n - 1), selected_idx + max(1, body_h - 1))
                if serials:
                    self.selected_serial = serials[new_idx]
                if new_idx >= n - 1:
                    self.follow = True
            elif key in (ord("g"), curses.KEY_HOME):
                self.follow = False
                if serials:
                    self.selected_serial = serials[0]
            elif key in (ord("G"), curses.KEY_END):
                self.follow = True
            elif key in (10, 13, curses.KEY_ENTER):
                if not rows:
                    continue
                rec = rows[selected_idx]
                result = run_editor(
                    stdscr,
                    rec,
                    header_attr=header_attr,
                    meta_attr=meta_attr,
                    error_attr=error_attr,
                    field_attr=sel_attr,
                    stream_properties_path=self.stream_properties_path,
                    debounce_path=self.debounce_path,
                )
                stdscr.nodelay(True)
                stdscr.timeout(120)
                if result is None:
                    self.status = "edit cancelled"
                    continue
                try:
                    on_progress = lambda step, total, label: draw_save_progress(
                        stdscr,
                        step,
                        total,
                        label,
                        header_attr=header_attr,
                        meta_attr=meta_attr,
                    )
                    if result.delete_entry:
                        self.status = delete_stream_settings(
                            result,
                            stream_properties_path=self.stream_properties_path,
                            on_progress=on_progress,
                        )
                        continue
                    self.status = save_stream_settings(
                        rec,
                        result,
                        stream_properties_path=self.stream_properties_path,
                        debounce_path=self.debounce_path,
                        debounce=self.monitor.debounce,
                        on_progress=on_progress,
                    )
                    linear = max(0.0, result.percent / 100.0)
                    cmap = rec.channel_map or ["FL", "FR"]
                    changes: dict[str, Any] = {
                        "mute": result.mute,
                        "volume_linear": linear,
                        "channel_map": cmap,
                        "channel_volumes": [linear] * max(1, len(cmap)),
                    }
                    if result.sink_name is not None:
                        changes["sink_name"] = result.sink_name
                    self.monitor.update_record(rec.serial, **changes)
                except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
                    self.status = f"save failed: {exc}"
            elif key == curses.KEY_RESIZE:
                pass


def install_desktop_launcher() -> int:
    """Install a user .desktop launcher (Terminal=true)."""
    root = xdg_data_root()
    apps_dir = root / "applications"
    svg_dir = root / "icons" / "hicolor" / "scalable" / "apps"
    png_dir = root / "icons" / "hicolor" / "256x256" / "apps"
    for path in (apps_dir, svg_dir, png_dir):
        path.mkdir(parents=True, exist_ok=True)

    pkg = files("pw_stream_memory.data")
    copies = (
        ("pw-stream-memory.svg", svg_dir / "pw-stream-memory.svg"),
        ("pw-stream-memory.png", png_dir / "pw-stream-memory.png"),
    )
    for name, dest in copies:
        with as_file(pkg.joinpath(name)) as src:
            dest.write_bytes(Path(src).read_bytes())
        print(dest)

    found = shutil.which("pw-stream-memory")
    if found:
        exec_line = found if " " not in found else f'"{found}"'
        try_exec = found
    else:
        exec_line = f"{sys.executable} -m pw_stream_memory"
        try_exec = sys.executable

    with as_file(pkg.joinpath("pw-stream-memory.desktop")) as src:
        text = Path(src).read_text(encoding="utf-8")
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("Exec="):
            lines.append(f"Exec={exec_line}")
        elif line.startswith("TryExec="):
            lines.append(f"TryExec={try_exec}")
        else:
            lines.append(line)
    desktop_path = apps_dir / "pw-stream-memory.desktop"
    desktop_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(desktop_path)

    updater = shutil.which("update-desktop-database")
    if updater:
        subprocess.run(
            [updater, str(apps_dir)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    print("Launcher installed.")
    return 0


def _copy_pkg_data(name: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    pkg = files("pw_stream_memory.data")
    with as_file(pkg.joinpath(name)) as src:
        dest.write_bytes(Path(src).read_bytes())
    print(dest)


def _print_wp_restart_hint() -> None:
    print("Restart WirePlumber to apply:")
    print("  systemctl --user restart wireplumber")
    print("This tool does not restart WirePlumber for you.")


def install_lua_hook() -> int:
    script_dest, conf_dest = lua_hook_install_paths()
    _copy_pkg_data(LUA_HOOK_SCRIPT, script_dest)
    _copy_pkg_data(LUA_HOOK_CONF, conf_dest)
    if default_overrides_path().is_file() or canonical_overrides_path().is_file():
        save_overrides(load_overrides())
    print("Lua hook files installed.")
    _print_wp_restart_hint()
    return 0


def uninstall_lua_hook() -> int:
    script_dest, conf_dest = lua_hook_install_paths()
    removed = False
    for path in (script_dest, conf_dest):
        if path.is_file():
            path.unlink()
            print(f"removed {path}")
            removed = True
        else:
            print(f"not installed: {path}")
    if not removed:
        print("Lua hook was not installed.")
    else:
        _print_wp_restart_hint()
    return 0


def _require_tty() -> None:
    if not sys.stdout.isatty() or not sys.stdin.isatty():
        print("This UI needs a real terminal (stdin and stdout as a tty).", file=sys.stderr)
        sys.exit(2)


def main(argv: list[str] | None = None) -> int:
    locale.setlocale(locale.LC_ALL, "")
    parser = argparse.ArgumentParser(
        prog="pw-stream-memory",
        description="Ncurses editor for PipeWire / WirePlumber per-app stream restore.",
    )
    from pw_stream_memory import __version__

    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "-f",
        "--history-file",
        type=Path,
        default=default_history_path(),
        help="JSON file for closed streams (default: %(default)s)",
    )
    parser.add_argument(
        "--stream-properties",
        type=Path,
        default=default_stream_properties_path(),
        help="WirePlumber stream-properties file (default: %(default)s)",
    )
    parser.add_argument(
        "--debounce-file",
        type=Path,
        default=default_debounce_path(),
        help="JSON file for per-app debounce flags (default: %(default)s)",
    )
    parser.add_argument(
        "--install-desktop",
        action="store_true",
        help="Install a user .desktop launcher (opens in a terminal on any freedesktop DE)",
    )
    parser.add_argument(
        "--install-lua-hook",
        action="store_true",
        help="Install the optional WirePlumber Lua hook for Match-by-binary (does not restart WirePlumber)",
    )
    parser.add_argument(
        "--uninstall-lua-hook",
        action="store_true",
        help="Remove the optional WirePlumber Lua hook files (does not restart WirePlumber)",
    )
    args = parser.parse_args(argv)

    if args.install_lua_hook and args.uninstall_lua_hook:
        print("Choose one of --install-lua-hook or --uninstall-lua-hook.", file=sys.stderr)
        return 2
    if args.install_desktop:
        return install_desktop_launcher()
    if args.install_lua_hook:
        return install_lua_hook()
    if args.uninstall_lua_hook:
        return uninstall_lua_hook()

    if shutil.which("pactl") is None:
        print("pactl not found in PATH.", file=sys.stderr)
        return 1
    _require_tty()

    debounce_path = args.debounce_file.expanduser()
    stream_properties_path = args.stream_properties.expanduser()
    debounce = DebounceEngine(debounce_path, stream_properties_path)
    monitor = PulseMonitor(args.history_file.expanduser(), debounce=debounce)
    monitor.start()

    def _handle_signal(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        tui = Tui(
            monitor,
            stream_properties_path=stream_properties_path,
            debounce_path=debounce_path,
        )
        curses.wrapper(tui.run)
    except KeyboardInterrupt:
        pass
    finally:
        monitor.stop()
    return 0


if __name__ == "__main__":
    os.environ.setdefault("ESCDELAY", "25")
    sys.exit(main())
