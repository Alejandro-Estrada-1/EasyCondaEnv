"""EasyCondaEnv helper.

This module wraps ``conda create`` and provides multiple user interfaces.

The available execution paths are:
- direct CLI execution through Click,
- Trogon form UI (when available),
- custom two-screen Textual wizard with package search/selection.
"""

import subprocess
import sys
import shutil
import shlex
import re
import json
import importlib
import threading
import queue
from dataclasses import dataclass
from pathlib import Path
import click

try:
    _packaging_version_module = importlib.import_module("packaging.version")
    Version = _packaging_version_module.Version
    InvalidVersion = _packaging_version_module.InvalidVersion
except ImportError:
    Version = None
    InvalidVersion = Exception

try:
    _trogon_module = importlib.import_module("trogon")
    tui = getattr(_trogon_module, "tui", None)
except ImportError:
    tui = None


TEXTUAL_UNAVAILABLE = object()


@dataclass
class CondaCreateConfig:
    """Normalized configuration used to assemble and run ``conda create``.

    Attributes:
        name: Environment name when using ``--name``.
        prefix: Full path to environment location when using ``--prefix``.
        python_version: Python version specifier (for example ``3.11``).
        channels: Additional channels to pass to conda.
        override_channels: Whether to use only explicitly provided channels.
        no_default_packages: Whether to ignore ``create_default_packages``.
        solver: Solver backend (``classic`` or ``libmamba``).
        file_specs: Package spec files used with ``--file``.
        yes: Whether to auto-confirm prompts using ``--yes``.
        dry_run: Whether to simulate creation without changes.
        packages: Additional package specs to install.
    """

    name: str = ""
    prefix: str = ""
    python_version: str = ""
    channels: tuple = ()
    override_channels: bool = False
    no_default_packages: bool = False
    solver: str = ""
    file_specs: tuple = ()
    yes: bool = True
    dry_run: bool = False
    packages: tuple = ()


def _validate_solver(ctx, param, value):
    """Validate and normalize the ``--solver`` option.

    Args:
        ctx: Click context passed by callback (unused).
        param: Click parameter object (unused).
        value: Raw ``--solver`` value.

    Returns:
        str | None: Normalized solver name or ``None``.

    Raises:
        click.BadParameter: If solver is not one of supported values.
    """

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
    """Normalize channel inputs while preserving order and uniqueness.

    Args:
        channels: Iterable containing raw channel values.

    Returns:
        tuple: Unique, ordered channel names.
    """

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


def _normalize_simple_tokens(raw_value):
    """Split a raw string by spaces/commas and return non-empty tokens.

    Args:
        raw_value: Raw string input.

    Returns:
        tuple: Parsed token values.
    """

    if not raw_value:
        return tuple()
    return tuple(part for part in re.split(r"[\s,]+", raw_value.strip()) if part)


def _normalize_file_specs(raw_value):
    """Normalize ``--file`` style values from comma/newline separated input.

    Args:
        raw_value: Raw spec input string.

    Returns:
        tuple: Unique spec file paths in input order.
    """

    if not raw_value:
        return tuple()

    normalized = []
    seen = set()
    for part in re.split(r"[\n,]+", raw_value):
        cleaned = part.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            normalized.append(cleaned)

    return tuple(normalized)


def _normalize_search_query(query):
    """Trim surrounding spaces from a package search query.

    Args:
        query: Raw user search text.

    Returns:
        str: Normalized query string.
    """

    cleaned = (query or "").strip()
    return cleaned


def _selected_pairs_to_specs(selected_pairs):
    """Convert selected package/version pairs into conda specs.

    Args:
        selected_pairs: Iterable of ``(name, version)`` tuples.

    Returns:
        tuple: Unique ``name=version`` specs.
    """

    specs = []
    seen = set()
    for name, version in selected_pairs:
        spec = f"{name}={version}"
        if spec not in seen:
            seen.add(spec)
            specs.append(spec)
    return tuple(specs)


def _natural_version_key(version):
    """Build fallback sortable key for versions without ``packaging`` support.

    Args:
        version: Raw version string.

    Returns:
        tuple: Comparable key suitable for natural version sorting.
    """

    key = []
    for token in re.split(r"([0-9]+)", version):
        if not token:
            continue
        if token.isdigit():
            key.append((1, int(token)))
        else:
            key.append((0, token.lower()))
    return tuple(key)


def _version_sort_key(version):
    """Return a comparable key for version sorting.

    Prefers ``packaging.version.Version`` (PEP 440) and falls back to
    ``_natural_version_key`` when parsing is unavailable.

    Args:
        version: Raw version string.

    Returns:
        tuple: Comparable key for ordering versions.
    """

    if Version is not None:
        try:
            return (1, Version(version))
        except InvalidVersion:
            pass
    return (0, _natural_version_key(version))


def _search_conda_packages(query, channels):
    """Search conda packages and return unique ``(name, version)`` entries.

    The search is optimized for responsiveness:
    - exact package names by default (``--full-name``),
    - wildcard mode only when user explicitly adds wildcard characters,
    - timeout protection and result capping.

    Args:
        query: Package search text from the UI.
        channels: Channel names to include in the search command.

    Returns:
        tuple[list[tuple[str, str]], str]:
            A pair containing results and an error message.
            The error message is empty on success.
    """

    search_query = _normalize_search_query(query)
    if not search_query:
        return [], "Write a package name to search."

    max_results = 800

    # Fast path: exact package name only, which avoids huge wildcard scans.
    attempts = [(search_query, True)]
    if any(char in search_query for char in "*?[]"):
        attempts = [(search_query, False)]

    errors = []
    for pattern, use_full_name in attempts:
        cmd = ["conda", "search", "--json"]
        if use_full_name:
            cmd.append("--full-name")
        cmd.append(pattern)

        for channel in channels:
            cmd.extend(["--channel", channel])

        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return [], "Search timed out after 30 seconds. Try a more specific name."

        payload = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()

        if not payload:
            if stderr:
                errors.append(stderr)
            continue

        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            errors.append(stderr or payload.splitlines()[-1])
            continue

        if isinstance(data, dict) and data.get("exception_name"):
            message = data.get("message") or data.get("error") or data.get("exception_name")
            if isinstance(message, list):
                message = " ".join(str(part) for part in message)
            errors.append(str(message))
            continue

        if not isinstance(data, dict):
            errors.append("Unexpected response from conda search.")
            continue

        entries = []
        seen = set()
        for pkg_name, records in data.items():
            if not isinstance(records, list):
                continue
            for record in records:
                if not isinstance(record, dict):
                    continue
                name = str(record.get("name") or pkg_name).strip()
                version = str(record.get("version") or "").strip()
                if not name or not version:
                    continue
                key = (name, version)
                if key in seen:
                    continue
                seen.add(key)
                entries.append(key)
                if len(entries) >= max_results:
                    break
            if len(entries) >= max_results:
                break

        # Keep package names grouped, but show newest versions first inside each package.
        entries.sort(key=lambda item: _version_sort_key(item[1]), reverse=True)
        entries.sort(key=lambda item: item[0].lower())
        if entries:
            return entries, ""

    if errors:
        return [], errors[-1]
    return [], "No package found for that name."

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
    """Assemble the final ``conda create`` command list.

    Args:
        name: Environment name.
        prefix: Environment path prefix.
        python_version: Python version specifier.
        channels: Extra channels.
        override_channels: Whether to pass ``--override-channels``.
        no_default_packages: Whether to pass ``--no-default-packages``.
        solver: Solver backend.
        file_specs: Spec files to pass with ``--file``.
        yes: Whether to auto-confirm prompts.
        dry_run: Whether to run conda in dry-run mode.
        packages: Extra package specs.

    Returns:
        list[str]: Complete command tokens for subprocess execution.
    """

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


def _execute_conda_create(config):
    """Execute ``conda create`` and stream output in real time.

    Args:
        config: Normalized create-environment configuration.

    Raises:
        click.ClickException: If conda is unavailable or command fails.
    """

    if shutil.which("conda") is None:
        raise click.ClickException("Conda not found in PATH.")

    cmd = _build_conda_create_command(
        name=config.name,
        prefix=config.prefix,
        python_version=config.python_version,
        channels=config.channels,
        override_channels=config.override_channels,
        no_default_packages=config.no_default_packages,
        solver=config.solver,
        file_specs=config.file_specs,
        yes=config.yes,
        dry_run=config.dry_run,
        packages=config.packages,
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

    if config.name:
        click.echo(f"Activate it with: conda activate {config.name}")
    if config.prefix:
        click.echo(f"Activate it with: conda activate {config.prefix}")


def _run_textual_wizard():
    """Run the custom Textual wizard and return a ``CondaCreateConfig``.

    Returns:
        CondaCreateConfig | None | object:
            - ``CondaCreateConfig`` when user confirms creation,
            - ``None`` when user exits without creating,
            - ``TEXTUAL_UNAVAILABLE`` when Textual cannot be imported.
    """

    try:
        textual_app = importlib.import_module("textual.app")
        textual_binding = importlib.import_module("textual.binding")
        textual_containers = importlib.import_module("textual.containers")
        textual_screen = importlib.import_module("textual.screen")
        textual_widgets = importlib.import_module("textual.widgets")

        App = textual_app.App
        Binding = textual_binding.Binding
        Horizontal = textual_containers.Horizontal
        Vertical = textual_containers.Vertical
        Screen = textual_screen.Screen
        Button = textual_widgets.Button
        Checkbox = textual_widgets.Checkbox
        Footer = textual_widgets.Footer
        Header = textual_widgets.Header
        Input = textual_widgets.Input
        Label = textual_widgets.Label
        ListItem = textual_widgets.ListItem
        ListView = textual_widgets.ListView
        Static = textual_widgets.Static
    except ImportError:
        return TEXTUAL_UNAVAILABLE

    class PackageSelectorScreen(Screen):
        """Second screen used to search and select package versions."""

        BINDINGS = [
            Binding("ctrl+b", "back_to_main", "Back to main"),
            Binding("ctrl+f", "focus_search", "Focus search"),
            Binding("delete", "remove_selected", "Remove selected"),
        ]

        def __init__(self, selected_pairs, channels):
            """Initialize selector state.

            Args:
                selected_pairs: Pre-selected package/version pairs.
                channels: Channels to use for package search.
            """
            super().__init__()
            self.selected_pairs = list(selected_pairs)
            self.channels = tuple(channels)
            self.search_results = []
            self.search_in_progress = False
            self.search_queue = queue.Queue()

        def compose(self):
            """Build the package selector layout."""
            yield Header(show_clock=True)
            yield Vertical(
                Static("Package selector"),
                Static("Search packages and versions. Enter adds from left to right. Ctrl+B returns."),
                Input(placeholder="Search package names (example: numpy)", id="pkg_search_input"),
                Button("Search", id="pkg_search_btn", variant="primary"),
                Horizontal(
                    Vertical(
                        Static("Results: name + version (no build)"),
                        ListView(id="pkg_results_list"),
                        id="pkg_left_panel",
                    ),
                    Vertical(
                        Static("Selected packages"),
                        ListView(id="pkg_selected_list"),
                        id="pkg_right_panel",
                    ),
                    id="pkg_lists",
                ),
                Static("", id="pkg_status"),
                id="pkg_root",
            )
            yield Footer()

        def on_mount(self):
            """Set initial focus and start polling for async search results."""
            self.query_one("#pkg_search_input", Input).focus()
            self._refresh_selected_list()
            self._set_status("Use Enter on a result to add it. Use Delete on right list to remove.")
            self.set_interval(0.2, self._drain_search_queue)

        def _set_status(self, message):
            """Update status line in the selector screen.

            Args:
                message: Status message to display.
            """
            self.query_one("#pkg_status", Static).update(message)

        def _clear_list(self, list_widget):
            """Remove all current rows from a ListView widget.

            Args:
                list_widget: Target list widget.
            """
            for child in list(list_widget.children):
                child.remove()

        def _refresh_results_list(self):
            """Render search results in the left panel."""
            list_widget = self.query_one("#pkg_results_list", ListView)
            self._clear_list(list_widget)
            for name, version in self.search_results:
                list_widget.append(ListItem(Label(f"{name} {version}")))

            if self.search_results:
                list_widget.index = 0

        def _refresh_selected_list(self):
            """Render selected package versions in the right panel."""
            list_widget = self.query_one("#pkg_selected_list", ListView)
            self._clear_list(list_widget)
            for name, version in self.selected_pairs:
                list_widget.append(ListItem(Label(f"{name} {version}")))

            if self.selected_pairs:
                list_widget.index = 0

        def _search(self):
            """Start package search in a background thread."""
            query = self.query_one("#pkg_search_input", Input).value
            if self.search_in_progress:
                self._set_status("Search already running. Please wait.")
                return

            if not query.strip():
                self._set_status("Write a package name. Use * manually for wildcard searches.")
                return

            self.search_in_progress = True
            self.query_one("#pkg_search_btn", Button).disabled = True
            self._set_status("Searching conda package index...")

            def _worker():
                try:
                    results, error = _search_conda_packages(query, self.channels)
                except Exception as exc:
                    results, error = [], f"Search failed: {exc}"

                self.search_queue.put((results, error))

            threading.Thread(target=_worker, daemon=True).start()

        def _drain_search_queue(self):
            """Move completed async search results into the UI thread."""
            while True:
                try:
                    results, error = self.search_queue.get_nowait()
                except queue.Empty:
                    break
                self._apply_search_results(results, error)

        def _apply_search_results(self, results, error):
            """Apply completed search results and restore selector state.

            Args:
                results: Search results as ``(name, version)`` tuples.
                error: Optional error message.
            """
            self.search_in_progress = False
            self.query_one("#pkg_search_btn", Button).disabled = False
            self.search_results = results
            self._refresh_results_list()

            if error:
                self._set_status(f"Search error: {error}")
            else:
                self._set_status(f"Found {len(results)} package versions.")

        def _add_current_result(self):
            """Add currently selected search result to the selected list."""
            list_widget = self.query_one("#pkg_results_list", ListView)
            if list_widget.index is None:
                self._set_status("No result selected.")
                return

            index = int(list_widget.index)
            if index < 0 or index >= len(self.search_results):
                self._set_status("Invalid result index.")
                return

            pair = self.search_results[index]
            if pair not in self.selected_pairs:
                self.selected_pairs.append(pair)
                self._refresh_selected_list()
                self._set_status(f"Added {pair[0]} {pair[1]}.")
            else:
                self._set_status(f"{pair[0]} {pair[1]} is already selected.")

        def action_remove_selected(self):
            """Remove the selected package version from the right panel list."""
            focused = self.focused
            selected_list = self.query_one("#pkg_selected_list", ListView)
            if focused is not selected_list:
                return
            if selected_list.index is None:
                return

            index = int(selected_list.index)
            if index < 0 or index >= len(self.selected_pairs):
                return

            removed = self.selected_pairs.pop(index)
            self._refresh_selected_list()
            self._set_status(f"Removed {removed[0]} {removed[1]}.")

        def action_focus_search(self):
            """Focus the search input field."""
            self.query_one("#pkg_search_input", Input).focus()

        def action_back_to_main(self):
            """Return to main screen, passing selected package versions back."""
            self.dismiss(tuple(self.selected_pairs))

        def on_button_pressed(self, event):
            """Handle selector button actions."""
            if event.button.id == "pkg_search_btn":
                self._search()

        def on_input_submitted(self, event):
            """Trigger search when user submits query input."""
            if event.input.id == "pkg_search_input":
                self._search()

        def on_list_view_selected(self, event):
            """Handle Enter/selection actions from list widgets."""
            if event.list_view.id == "pkg_results_list":
                self._add_current_result()

    class CondaWizardApp(App):
        """Main TUI wizard for environment options and command preview."""

        CSS = """
        #main_root {
            layout: horizontal;
            height: 1fr;
        }

        #main_form, #main_preview {
            width: 1fr;
            height: 1fr;
            padding: 1 2;
            border: round $primary;
        }

        #main_form {
            overflow-y: auto;
        }

        #main_preview {
            border: round $success;
        }

        #cmd_preview, #selected_summary {
            height: 1fr;
            overflow-y: auto;
            border: round $accent;
            padding: 1;
        }

        #main_status {
            min-height: 3;
            border: round $warning;
            padding: 0 1;
        }

        #pkg_root {
            padding: 1 2;
            height: 1fr;
        }

        #pkg_lists {
            height: 1fr;
            layout: horizontal;
            margin-top: 1;
        }

        #pkg_left_panel, #pkg_right_panel {
            width: 1fr;
            height: 1fr;
            border: round $primary;
            padding: 1;
        }

        #pkg_status {
            min-height: 3;
            border: round $warning;
            padding: 0 1;
            margin-top: 1;
        }
        """

        BINDINGS = [
            Binding("ctrl+o", "open_package_selector", "Packages"),
            Binding("ctrl+c", "create_environment", "Create"),
            Binding("ctrl+q", "quit_wizard", "Quit"),
        ]

        def __init__(self):
            """Initialize main wizard state."""
            super().__init__()
            self.selected_pairs = []

        def compose(self):
            """Build the two-panel main wizard layout."""
            yield Header(show_clock=True)
            yield Horizontal(
                Vertical(
                    Static("Main interface"),
                    Static("Use Ctrl+O for package selector, Ctrl+C to create, Ctrl+Q to exit."),
                    Input(placeholder="Environment name", id="name_input"),
                    Input(placeholder="Environment prefix path", id="prefix_input"),
                    Input(placeholder="Python version (example: 3.11)", id="python_input"),
                    Input(placeholder="Channels (space/comma separated)", id="channels_input"),
                    Input(placeholder="Spec files (comma/newline separated)", id="files_input"),
                    Input(placeholder="Solver: classic or libmamba", id="solver_input"),
                    Input(placeholder="Extra package specs (space/comma separated)", id="manual_packages_input"),
                    Checkbox("Override channels", id="override_checkbox"),
                    Checkbox("No default packages", id="no_default_checkbox"),
                    Checkbox("Auto confirm (--yes)", value=True, id="yes_checkbox"),
                    Checkbox("Dry run", id="dry_run_checkbox"),
                    Button("Open package selector", id="open_pkg_btn", variant="primary"),
                    Button("Create environment", id="create_btn", variant="success"),
                    Button("Exit", id="exit_btn", variant="error"),
                    id="main_form",
                ),
                Vertical(
                    Static("Selected packages (name version)"),
                    Static("None selected.", id="selected_summary"),
                    Static("Conda create command preview"),
                    Static("", id="cmd_preview"),
                    Static("", id="main_status"),
                    id="main_preview",
                ),
                id="main_root",
            )
            yield Footer()

        def on_mount(self):
            """Set initial focus and render first command preview."""
            self.query_one("#name_input", Input).focus()
            self._update_preview()

        def _set_status(self, message):
            """Update status line in the main wizard.

            Args:
                message: Status message to display.
            """
            self.query_one("#main_status", Static).update(message)

        def _collect_config(self, validate=False):
            """Collect form values and produce a normalized config object.

            Args:
                validate: When ``True``, enforce strict input checks.

            Returns:
                tuple[CondaCreateConfig | None, str]:
                    Parsed config and validation error message.
            """
            name = self.query_one("#name_input", Input).value.strip()
            prefix = self.query_one("#prefix_input", Input).value.strip()
            python_version = self.query_one("#python_input", Input).value.strip()
            channels = _normalize_channels((self.query_one("#channels_input", Input).value,))
            file_specs = _normalize_file_specs(self.query_one("#files_input", Input).value)
            solver = self.query_one("#solver_input", Input).value.strip().lower()
            manual_packages = _normalize_simple_tokens(self.query_one("#manual_packages_input", Input).value)
            override_channels = self.query_one("#override_checkbox", Checkbox).value
            no_default_packages = self.query_one("#no_default_checkbox", Checkbox).value
            yes = self.query_one("#yes_checkbox", Checkbox).value
            dry_run = self.query_one("#dry_run_checkbox", Checkbox).value

            if solver and solver not in {"classic", "libmamba"}:
                return None, "Solver must be classic or libmamba."

            if validate:
                if name and prefix:
                    return None, "Use either name or prefix, not both."
                if not name and not prefix:
                    return None, "Provide either an environment name or prefix."
                for spec in file_specs:
                    spec_path = Path(spec)
                    if not spec_path.is_file():
                        return None, f"Spec file not found: {spec}"

            selected_specs = _selected_pairs_to_specs(self.selected_pairs)
            all_packages = []
            seen = set()
            for spec in tuple(selected_specs) + tuple(manual_packages):
                if spec not in seen:
                    seen.add(spec)
                    all_packages.append(spec)

            config = CondaCreateConfig(
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
                packages=tuple(all_packages),
            )

            return config, ""

        def _update_selected_summary(self):
            """Render selected package versions in the summary panel."""
            summary_widget = self.query_one("#selected_summary", Static)
            if not self.selected_pairs:
                summary_widget.update("None selected.")
                return

            lines = [f"{name} {version}" for name, version in self.selected_pairs]
            summary_widget.update("\n".join(lines))

        def _update_preview(self):
            """Rebuild and display the live ``conda create`` command preview."""
            config, error = self._collect_config(validate=False)
            self._update_selected_summary()
            if error:
                self.query_one("#cmd_preview", Static).update("Cannot preview command: " + error)
                return

            cmd = _build_conda_create_command(
                name=config.name,
                prefix=config.prefix,
                python_version=config.python_version,
                channels=config.channels,
                override_channels=config.override_channels,
                no_default_packages=config.no_default_packages,
                solver=config.solver,
                file_specs=config.file_specs,
                yes=config.yes,
                dry_run=config.dry_run,
                packages=config.packages,
            )
            self.query_one("#cmd_preview", Static).update(shlex.join(cmd))

        def _open_package_selector(self):
            """Open package selector screen and register callback."""
            channels = _normalize_channels((self.query_one("#channels_input", Input).value,))
            self.push_screen(
                PackageSelectorScreen(self.selected_pairs, channels),
                self._package_selector_callback,
            )

        def _package_selector_callback(self, selected_pairs):
            """Receive selected packages from selector and refresh preview.

            Args:
                selected_pairs: Selected ``(name, version)`` tuples or ``None``.
            """
            if selected_pairs is None:
                return
            self.selected_pairs = list(selected_pairs)
            self._set_status(f"Loaded {len(self.selected_pairs)} selected package versions.")
            self._update_preview()

        def action_open_package_selector(self):
            """Keyboard action to open package selector."""
            self._open_package_selector()

        def action_create_environment(self):
            """Validate data and exit app with config for execution."""
            config, error = self._collect_config(validate=True)
            if error:
                self._set_status(error)
                self._update_preview()
                return

            self.exit(config)

        def action_quit_wizard(self):
            """Exit wizard without creating the environment."""
            self.exit(None)

        def on_button_pressed(self, event):
            """Handle main wizard buttons."""
            if event.button.id == "open_pkg_btn":
                self._open_package_selector()
            elif event.button.id == "create_btn":
                self.action_create_environment()
            elif event.button.id == "exit_btn":
                self.action_quit_wizard()

        def on_input_changed(self, event):
            """Refresh command preview after relevant input changes."""
            tracked_ids = {
                "name_input",
                "prefix_input",
                "python_input",
                "channels_input",
                "files_input",
                "solver_input",
                "manual_packages_input",
            }
            if event.input.id in tracked_ids:
                self._update_preview()

        def on_checkbox_changed(self, event):
            """Refresh command preview after relevant checkbox changes."""
            tracked_ids = {
                "override_checkbox",
                "no_default_checkbox",
                "yes_checkbox",
                "dry_run_checkbox",
            }
            if event.checkbox.id in tracked_ids:
                self._update_preview()

    app = CondaWizardApp()
    return app.run()


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
    """Click entry point for non-Textual execution paths.

    Args:
        name: Environment name.
        prefix: Environment path prefix.
        python_version: Python version specifier.
        channels: Additional channels.
        override_channels: Whether to ignore default channels.
        no_default_packages: Whether to ignore default package config.
        solver: Conda solver backend.
        file_specs: Spec files used with ``--file``.
        yes: Whether to auto-confirm prompts.
        dry_run: Whether to run in dry-run mode.
        packages: Positional package specs.

    Raises:
        click.UsageError: If incompatible options are provided.
        click.ClickException: If conda execution fails.
    """

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

    config = CondaCreateConfig(
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

    _execute_conda_create(config)


if tui is not None:
    run_string = f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))}"
    cli = tui(name=run_string)(cli)


def main():
    """Program entry point selecting Textual, Trogon, or CLI execution path."""

    if tui is not None and len(sys.argv) == 1:
        selected_config = _run_textual_wizard()
        if selected_config is TEXTUAL_UNAVAILABLE:
            sys.argv.append("tui")
            cli()
            return
        if selected_config is None:
            print("No changes were applied.")
            return

        _execute_conda_create(selected_config)
        return

    if tui is None and len(sys.argv) == 1:
        print("Trogon is not installed; using interactive CLI mode.")
        print("Install optional TUI dependencies with: pip install -r requirements.txt")

    cli()

if __name__ == "__main__":
    main()
