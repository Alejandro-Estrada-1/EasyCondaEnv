# EasyCondaEnv
Interactive helper to create Conda environments using a two-screen TUI with package search, version selection, and live command preview.

## Files

- `conda_env_tui.py`: CLI/TUI helper to create Conda environments.
- `requirements.txt`: Python dependencies required to run the helper.

## Requirements

- Python 3.8+
- Conda available in `PATH`
- Optional UI dependencies from `requirements.txt` (mainly [Trogon](https://github.com/Textualize/trogon))

## Quick setup

```bash
python3 -m venv venv
source venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
```

> [!IMPORTANT]
> Activate your virtual environment before installing requirements, or UI dependencies may be installed in the wrong Python environment.

## Usage

### Default mode (`python3 conda_env_tui.py`)

```bash
python3 conda_env_tui.py
```

When UI dependencies are available, this starts the main TUI wizard (with live command preview) and includes a dedicated package selector screen.
If the custom Textual screen is not available, it automatically falls back to the Trogon form UI.

> [!NOTE]
> Running the script with command-line arguments skips the wizard and uses direct CLI mode.

### CLI mode

```bash
python3 conda_env_tui.py --name myenv --python 3.11
python3 conda_env_tui.py --prefix /tmp/myenv --channel conda-forge --channel bioconda samtools
python3 conda_env_tui.py --name bio --file envs/structural_biology.txt --dry-run
```

## How `conda_env_tui.py` works

`conda_env_tui.py` is a wrapper around `conda create` with these interfaces:

1. **Main TUI wizard** (when started with no args and UI dependencies are available).
2. **Trogon form UI** fallback when the custom Textual screen is unavailable.
3. **Interactive CLI mode** when TUI dependencies are not available.

### Package selector screen (second interface)

From the main TUI, open the package selector using `Ctrl+O` (or the button).

Main wizard shortcuts:

- `Ctrl+O`: open package selector.
- `Ctrl+C`: create environment.
- `Ctrl+Q`: exit wizard.

- Left panel: search results as `package version` (build strings are intentionally hidden).
- Right panel: selected package versions.
- `Enter` on a left result adds it to the right list.
- `Delete` on the right list removes the selected package.
- `Ctrl+B` returns to the main screen.

Search behavior:

- Uses exact package name by default for speed.
- Supports wildcards only when explicitly provided (for example `numpy*`).
- Results are de-duplicated by `(name, version)`.
- Versions are sorted from newest to oldest.

> [!TIP]
> For fastest results, start with an exact package name (for example `numpy`). Use wildcards only when needed.

After returning to the main screen, selected packages are merged into the `conda create` command preview and used for final environment creation.

Execution flow:

1. Validates key inputs (for example, `--solver` only accepts `classic` or `libmamba`).
2. Normalizes channels passed with `--channel` (supports repeated values and comma/space-separated input).
3. Ensures exactly one target is used: `--name` **or** `--prefix`.
4. Builds the final `conda create` command with your selected options:
	- channels,
	- solver,
	- `--file` package specs,
	- optional `python=<version>`,
	- package list from CLI arguments.
5. Runs Conda and streams output in real time.
6. Prints the proper activation command at the end.

If [trogon](https://github.com/Textualize/trogon) is missing, the script still works in CLI mode and shows a hint to install optional UI dependencies.

### Why `requirements.txt` is still needed

This project uses `requirements.txt` to install Python-side dependencies for the helper itself (mainly `click` and optional `trogon`) **after** creating and activating your local virtual environment:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Without this step, TUI mode will not be available.

## Main options

- `--name`: environment name.
- `--prefix`: full environment path.
- `--python`: Python version (e.g. `3.11`).
- `--channel`: additional channel (repeatable).
- `--override-channels`: use only provided channels.
- `--no-default-packages`: ignore `create_default_packages`.
- `--solver`: `classic` or `libmamba`.
- `--file`: package spec file (repeatable).
- `--dry-run`: simulate creation without making changes.

## Notes

- If [trogon](https://github.com/Textualize/trogon) is not installed, the script automatically falls back to interactive CLI mode.
- In the package selector, search runs asynchronously to keep the interface responsive.
- In small screens, the main interface panel is scrollable so action buttons remain reachable.
- The `venv/` folder is local development state and should not be versioned.

> [!IMPORTANT]
> Keep `venv/` out of version control (for example via `.gitignore`) to avoid committing local machine-specific files.
