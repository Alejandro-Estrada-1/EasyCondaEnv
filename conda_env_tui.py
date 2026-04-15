import subprocess
import sys
import shutil
import shlex
import re
from pathlib import Path
import click

try:
    from trogon import tui
except ImportError: 
    tui = None


def _validate_solver(ctx, param, value):
    if value is None:
        return None

    allowed = {"classic", "libmamba"}
    normalized = value.strip().lower()
    if not normalized:
        return None
    if normalized not in allowed:
        raise click.BadParameter("must be one of: classic, libmamba")
    return normalized


def _normalize_channels(channels):
    normalized = []
    seen = set()

    for value in channels:
        if not value:
            continue
        # Trogon may send multiple channels as a single string.
        parts = [part.strip() for part in re.split(r"[\s,]+", value) if part.strip()]
        for part in parts:
            if part not in seen:
                seen.add(part)
                normalized.append(part)

    return tuple(normalized)

def _build_conda_create_command(
    name,
    prefix,
    python_version,
    channels,
    override_channels,
    no_default_packages,
    solver,
    file_specs,
    yes,
    dry_run,
    packages,
):
    cmd = ["conda", "create"]

    if name:
        cmd.extend(["--name", name])
    if prefix:
        cmd.extend(["--prefix", prefix])

    for channel in channels:
        cmd.extend(["--channel", channel])

    if override_channels:
        cmd.append("--override-channels")
    if no_default_packages:
        cmd.append("--no-default-packages")
    if solver:
        cmd.extend(["--solver", solver])

    for spec_file in file_specs:
        cmd.extend(["--file", spec_file])

    if dry_run:
        cmd.append("--dry-run")
    if yes:
        cmd.append("--yes")

    if python_version:
        cmd.append(f"python={python_version}")

    cmd.extend(packages)
    return cmd


@click.command(help="Interactive Conda environment creator")
@click.option("--name", "name", help="Name of the environment")
@click.option("--prefix", "prefix", help="Full path to the environment location")
@click.option("--python", "python_version", help="Python version (e.g. 3.11)")
@click.option(
    "--channel",
    "channels",
    multiple=True,
    help="Additional channel (can be used multiple times)",
)
@click.option(
    "--override-channels",
    is_flag=True,
    help="Do not search default channels; use only provided channels",
)
@click.option(
    "--no-default-packages",
    is_flag=True,
    help="Ignore create_default_packages from condarc",
)
@click.option(
    "--solver",
    callback=_validate_solver,
    help="Conda solver backend",
)
@click.option(
    "--file",
    "file_specs",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Read package versions from file",
)
@click.option("--yes/--no-yes", default=True, show_default=True, help="Auto-confirm")
@click.option("--dry-run", is_flag=True, help="Only display what would be done")
@click.argument("packages", nargs=-1)
def cli(
    name,
    prefix,
    python_version,
    channels,
    override_channels,
    no_default_packages,
    solver,
    file_specs,
    yes,
    dry_run,
    packages,
):
    channels = _normalize_channels(channels)

    if not name and not prefix:
        if tui is None:
            name = click.prompt("Environment name")
        else:
            raise click.UsageError("Use --name or --prefix.")
    if name and prefix:
        raise click.UsageError("Use either --name or --prefix, not both.")

    if not python_version and tui is None:
        python_version = click.prompt(
            "Python version (optional)",
            default="",
            show_default=False,
        )

    if shutil.which("conda") is None:
        raise click.ClickException("Conda not found in PATH.")

    cmd = _build_conda_create_command(
        name=name,
        prefix=prefix,
        python_version=python_version,
        channels=channels,
        override_channels=override_channels,
        no_default_packages=no_default_packages,
        solver=solver,
        file_specs=file_specs,
        yes=yes,
        dry_run=dry_run,
        packages=packages,
    )

    click.echo("Running: " + " ".join(cmd))
    click.echo("Conda may take a while while solving dependencies...\n")

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    assert process.stdout is not None
    for line in process.stdout:
        click.echo(line, nl=False)

    return_code = process.wait()
    if return_code != 0:
        raise click.ClickException(f"Failed to create environment (exit code {return_code})")

    if name:
        click.echo(f"Activate it with: conda activate {name}")
    if prefix:
        click.echo(f"Activate it with: conda activate {prefix}")


if tui is not None:
    run_string = f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))}"
    cli = tui(name=run_string)(cli)


def main():
    if tui is not None and len(sys.argv) == 1:
        sys.argv.append("tui")

    if tui is None and len(sys.argv) == 1:
        print("Trogon is not installed; using interactive CLI mode.")
        print("Install optional TUI dependencies with: pip install -r requirements.txt")

    cli()

if __name__ == "__main__":
    main()
