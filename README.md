# EasyCondaEnv
Create a conda env's with TUI

## Files

- `conda_env_tui.py`: CLI/TUI helper to create Conda environments.
- `requirements.txt`: Python dependencies required to run the helper.

## Requirements

- Python 3.8+
- Conda available in `PATH`

## Quick setup

From this directory (`bin/python/`):

```bash
python3 -m venv venv
source venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
```

## Usage

### TUI mode (if `trogon` is installed)

```bash
python3 conda_env_tui.py
```

### CLI mode

```bash
python3 conda_env_tui.py --name myenv --python 3.11
python3 conda_env_tui.py --prefix /tmp/myenv --channel conda-forge --channel bioconda samtools
python3 conda_env_tui.py --name bio --file envs/structural_biology.txt --dry-run
```

## How `conda_env_tui.py` works

`conda_env_tui.py` is a wrapper around `conda create` with two interfaces:

1. **TUI mode** (Text User Interface) when [trogon](https://github.com/Textualize/trogon) is installed.
2. **Interactive CLI mode** when [trogon](https://github.com/Textualize/trogon) is not available.

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
- The `venv/` folder is local development state and should not be versioned.
