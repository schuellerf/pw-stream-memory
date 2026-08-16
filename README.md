# pw-stream-memory

<img src="https://raw.githubusercontent.com/schuellerf/pw-stream-memory/main/assets/logo.png" width="160" alt="pw-stream-memory logo">

A terminal UI for **PipeWire + WirePlumber** that remembers per-application
playback volume, mute, and output device after the stream is gone.

Desktop volume applets (including KDE Plasma Sound settings) only show *live*
sink-inputs. Short sounds (`paplay`, a system bell, a notification) appear and
vanish before you can set them. This tool keeps a history of those streams and
writes WirePlumber’s native `stream-properties` so the next play restores what
you chose.

It is **not KDE-specific**. It uses `pactl` (PipeWire’s PulseAudio
compatibility) for live streams and WirePlumber for persistence. Classic
PulseAudio without PipeWire/WirePlumber can list and change live streams, but
cannot store restore state the way this program does.

## Install

From PyPI (after the first release):

```bash
pipx install pw-stream-memory
pw-stream-memory
```

From a git checkout (editable):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
pw-stream-memory
```

Or without a venv: `pipx install -e .`

### Desktop launcher

`Terminal=true` in the `.desktop` file is [freedesktop.org](https://specifications.freedesktop.org/desktop-entry-spec/latest/), so KDE, GNOME, and other DEs should open it in a terminal. After the command is on your `PATH`:

```bash
pw-stream-memory --install-desktop
```

That installs:

- `~/.local/share/applications/pw-stream-memory.desktop`
- `~/.local/share/icons/hicolor/scalable/apps/pw-stream-memory.svg`
- `~/.local/share/icons/hicolor/256x256/apps/pw-stream-memory.png`

Then look for **pw-stream-memory** in the application menu (Audio / Settings).

## Requirements

- Python 3.11+ (uses `from __future__ import annotations` style; tested on 3.14)
- a tty (ncurses)
- `pactl` (from PipeWire’s pulse tools)
- `wpctl` (WirePlumber), for saving restore state
- `pw-cli` (optional), so debounce can re-arm when a node goes idle then runs
  again

## Run

```bash
pw-stream-memory
```

From the source tree without installing:

```bash
PYTHONPATH=src python3 -m pw_stream_memory
```

```
pw-stream-memory  debounce 500ms/30s   q quit   ↑↓ select   Enter edit
```

The title bar shows the live debounce timings (`PW_STREAM_MEMORY_DEBOUNCE_ON` ms, then `PW_STREAM_MEMORY_DEBOUNCE_OFF` seconds) so you can confirm env overrides took effect.

Closed streams are oldest-first at the top; active streams stay at the bottom.

## Features

### Stream history

- Subscribes to Pulse sink-input changes and polls `pactl list sink-inputs`
- Records start/end time, label, mute, corked, volume, sink, and properties
- Reloads closed rows from JSON on the next launch

### Editor (Enter)

- **Match by** — WirePlumber identity (`media.role`, `application.id`,
  `application.name`, `media.name`, `node.name`). Stock WirePlumber restores
  only its default key (first of those that is set).
- **Volume** — 0–150%, same cubic percent as Pulse/KDE. Stored as linear
  `channelVolumes` in WirePlumber.
- **Sink** — pin to a device, or **default (no pin)** if you never chose one
- **Mute**
- **Debounce** — per identity, see below
- **WP restore** — shows whether `stream-properties` already has an entry
- **d / Del** — confirm, delete that restore entry, leave the editor
- **Enter** — save; **Esc** — cancel

### Native WirePlumber save

Saves go through WirePlumber’s own file, not a side-car hook:

1. disable `node.stream.restore-props` and `node.stream.restore-target`
2. wait for a pending WP flush (mtime, or 1s)
3. merge or delete the `stream-properties` entry
4. enable restore again

Runtime `wpctl settings` only — no `--save`. A progress screen shows `1/4` …
`4/4`. If the stream is still live, volume/mute/sink are also applied with
`pactl`.

### Debounce (optional)

When an identity has debounce on and a **new** stream appears:

1. default volume for `PW_STREAM_MEMORY_DEBOUNCE_ON` ms (default 500)
2. mute for `PW_STREAM_MEMORY_DEBOUNCE_OFF` seconds (default 60)
3. default volume again

After that it waits for the PipeWire node to go idle/suspended, then `running`
(or uncork) to start the cycle again. Debounce only runs while this UI is open.

The old names `SOUND_OVERRIDER_DEBOUNCE_ON` / `_OFF` still work as aliases.

## Files

| Path | What |
|---|---|
| `~/.local/state/pw-stream-memory/closed-streams.json` | closed-stream history |
| `~/.local/state/pw-stream-memory/debounce.json` | which identities have debounce |
| `~/.local/state/wireplumber/stream-properties` | WirePlumber restore database |

If the new state directory does not exist yet, history and debounce still load
from `~/.local/state/kde_sound_overrider/` (the previous name).

## Releasing to PyPI

Releases are automated: **bump the version, merge to `main`**. CI publishes that
version to PyPI and creates a GitHub tag `vX.Y.Z` plus a GitHub Release. If
`vX.Y.Z` already exists, the release job does nothing (so ordinary commits on
`main` without a version bump are safe).

### One-time setup

1. **Create the GitHub repo** and push this project. Then set `[project.urls]`
   in `pyproject.toml` to that repo.
2. **Create a GitHub Environment** named `pypi` (Settings → Environments).
   No secrets are required. You can add required reviewers if you want a human
   gate before each publish.
3. **PyPI account** at [pypi.org](https://pypi.org/account/register/) with
   2FA enabled.
4. **Trusted publisher** (no API token): PyPI → Your account → Publishing →
   *Add a new pending publisher*:
   - PyPI project name: `pw-stream-memory`
   - Owner: your GitHub user or org
   - Repository name: the GitHub repo name
   - Workflow name: `release.yml`
   - Environment name: `pypi`
5. The **first merge to `main` with version `0.1.0`** creates the PyPI project
   and uploads the package. After that, the pending publisher becomes a normal
   trusted publisher.

Do not put a PyPI password or token in GitHub secrets. Trusted publishing uses
OIDC (`id-token: write` in `.github/workflows/release.yml`).

### Each release

1. In a PR, set `[project].version` in `pyproject.toml` (for example `0.1.0` →
   `0.1.1`). Follow [semver](https://semver.org/): patch for fixes, minor for
   features, major for breaking changes.
2. Merge the PR to `main`.
3. Check the **Release** workflow, the `v*` tag, the GitHub Release, and
   [pypi.org/project/pw-stream-memory](https://pypi.org/project/pw-stream-memory/).

Manual upload (only if you are not using CI yet):

```bash
python3 -m pip install -e ".[dev]"
python3 -m build
python3 -m twine upload dist/*
```

## License

MIT. See [LICENSE](LICENSE).
