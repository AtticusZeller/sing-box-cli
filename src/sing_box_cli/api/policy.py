from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import FormattedTextControl, Layout as PTLayout, Window
from rich import box
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .client import GroupInfo, SingBoxAPIClient
from .widget import create_header_panel, render_rich_to_prompt_toolkit


def create_policy_group_table(
    group_data: GroupInfo | None,
    delay_cache: dict[str, int] | None = None,
    selected_index: int = -1,
) -> Panel:
    """Create a table to display policy group information."""
    if group_data is None:
        return Panel("No policy group data available", title="Policy Group")

    name = group_data.name
    type_info = group_data.type
    current = group_data.now
    proxies = group_data.all
    # Get delay information, prioritizing the cache
    history = group_data.history

    # Create table for proxies
    table = Table(expand=True, show_header=True)
    table.add_column("Proxy", style="cyan")
    table.add_column("Latency", style="green", justify="right")
    table.add_column("Status", style="yellow", justify="center")

    # Add proxies to table
    for i, proxy in enumerate(proxies):
        # Get latency from cache if available, otherwise from history
        latency = "Unknown"
        if delay_cache and proxy in delay_cache:
            delay = delay_cache[proxy]
            if delay > 0:
                latency = f"{delay} ms"
            else:
                latency = "Timeout"
        elif i == group_data.all.index(current):
            # [0]->latest delay for the current proxy
            delay = history[0].delay if history else -1
            if delay > 0:
                latency = f"{delay} ms"
            else:
                latency = "Timeout"

        # Determine status
        status = "✓" if proxy == current else ""

        # Apply style based on selection and current status
        if i == selected_index:
            # Selected item
            proxy_text = Text(proxy, style="bold white on blue")
            latency_text = Text(latency, style="bold white on blue")
            status_text = Text(status, style="bold white on blue")
            table.add_row(proxy_text, latency_text, status_text)
        elif proxy == current:
            # Current proxy (but not selected)
            table.add_row(
                Text(proxy, style="bold"),
                Text(latency, style="bold"),
                Text(status, style="bold"),
            )
        else:
            # Normal proxy
            table.add_row(proxy, latency, status)

    # Create header
    header = f"Policy Group: {name} ({type_info})"
    return Panel(table, title=header, subtitle=f"Current Selection: {current}")


class PolicyGroupManager:
    """Interactive policy group manager for Sing-Box."""

    def __init__(self, api_client: SingBoxAPIClient) -> None:
        """Initialize the policy group manager."""
        self.api_client = api_client
        self.groups: list[GroupInfo] = []
        self.selected_group_index = 0
        self.selected_proxy_index = 0
        self.current_group: GroupInfo | None = None
        self.status_message = "Loading policy groups..."

        # Add a delay cache to store test results
        # Format: {group_name: {proxy_name: delay_value}}
        self.delay_cache: dict[str, dict[str, int]] = {}

        # Setup components
        self.kb = KeyBindings()
        self.setup_keybindings()

        # Create application
        self.layout = self.create_layout()
        self.focus_on_groups = True
        self.app: Application[None] = Application(
            layout=self.layout,
            key_bindings=self.kb,
            mouse_support=False,
            full_screen=True,
            refresh_interval=0.5,
        )

    def setup_keybindings(self) -> None:
        """Setup keyboard shortcuts."""

        @self.kb.add("c-c")
        @self.kb.add("c-q")
        @self.kb.add(Keys.SIGINT)
        def _(event) -> None:  # type: ignore[no-untyped-def] # noqa: ARG001
            """Quit the application."""
            event.app.exit()

        @self.kb.add("tab")
        def _(event) -> None:  # type: ignore[no-untyped-def] # noqa: ARG001
            """Switch between group list and proxy list."""
            if self.focus_on_groups:
                self.focus_on_groups = False
                # initiate a test for the selected group
                if self.current_group:
                    event.app.create_background_task(
                        self.test_group_delay(self.current_group.name)
                    )
            else:
                self.focus_on_groups = True

        @self.kb.add("down")
        def _(event) -> None:  # type: ignore[no-untyped-def] # noqa: ARG001
            """Move selection down."""
            if self.focus_on_groups:
                if self.selected_group_index < len(self.groups) - 1:
                    self.selected_group_index += 1
                    event.app.create_background_task(self.load_selected_group())
            else:
                if self.current_group:
                    proxies = self.current_group.all
                    if self.selected_proxy_index < len(proxies) - 1:
                        self.selected_proxy_index += 1

        @self.kb.add("up")
        def _(event) -> None:  # type: ignore[no-untyped-def] # noqa: ARG001
            """Move selection up."""
            if self.focus_on_groups:
                if self.selected_group_index > 0:
                    self.selected_group_index -= 1
                    event.app.create_background_task(self.load_selected_group())
            else:
                if self.selected_proxy_index > 0:
                    self.selected_proxy_index -= 1

        @self.kb.add("enter")
        def _(event) -> None:  # type: ignore[no-untyped-def] # noqa: ARG001
            """Select a group or a proxy."""
            if not self.focus_on_groups and self.current_group:
                # Select proxy
                group_name = self.current_group.name
                proxies = self.current_group.all
                if proxies and self.selected_proxy_index < len(proxies):
                    selected_proxy = proxies[self.selected_proxy_index]
                    event.app.create_background_task(
                        self.select_proxy(group_name, selected_proxy)
                    )

        @self.kb.add("t")
        def _(event) -> None:  # type: ignore[no-untyped-def] # noqa: ARG001
            """Test delay for selected proxy."""
            if not self.focus_on_groups and self.current_group:
                proxies = self.current_group.all
                if proxies and self.selected_proxy_index < len(proxies):
                    selected_proxy = proxies[self.selected_proxy_index]
                    event.app.create_background_task(
                        self.test_proxy_delay(selected_proxy)
                    )

        @self.kb.add("g")
        def _(event) -> None:  # type: ignore[no-untyped-def] # noqa: ARG001
            """Test delay for all proxies in the group."""
            if self.current_group:
                group_name = self.current_group.name
                event.app.create_background_task(self.test_group_delay(group_name))

    def create_layout(self) -> PTLayout:
        """Create the application layout using Rich Layout."""
        # Create a prompt_toolkit control that will render our rich layout
        self.rich_control = FormattedTextControl(self.get_rich_layout)

        # Create a single window that takes the full screen
        main_window = Window(content=self.rich_control)

        # Return the PT layout with just our rich window
        return PTLayout(main_window)

    def get_rich_layout(self) -> FormattedText:
        """Generate a rich layout and convert it to prompt_toolkit format."""
        # Create Rich layout
        layout = Layout()

        # Split into header, main content, and footer
        layout.split(
            Layout(name="header", size=3),
            Layout(name="main"),
            Layout(name="footer", size=3),
        )

        # Split main area into groups and proxies
        layout["main"].split_row(
            Layout(name="groups", ratio=1, minimum_size=15),
            Layout(name="proxies", ratio=4),
        )

        # Create tables and panels
        header_panel = create_header_panel("Sing-Box Policy Group Manager")

        # Create groups panel
        groups_panel = self.create_groups_panel()

        # Create proxies panel
        proxies_panel = self.create_proxies_panel()

        footer_panel = Panel(
            Text(
                "Ctrl+C/Ctrl+Q: Quit | Tab: Switch focus | Enter: Select proxy | "
                "T: Test proxy | G: Test group | "
                f"Status: {self.status_message}",
                justify="center",
                no_wrap=True,
            ),
            style="bright_white on #333333",
            box=box.SIMPLE,  # No border
            padding=0,  # No padding
        )

        # Update all layout sections
        layout["header"].update(header_panel)
        layout["groups"].update(groups_panel)
        layout["proxies"].update(proxies_panel)
        layout["footer"].update(footer_panel)

        return render_rich_to_prompt_toolkit(layout)

    def create_groups_panel(self) -> Panel:
        """Create a panel for policy groups."""
        # Create a rich table for groups
        table = Table(box=box.SIMPLE, expand=True, show_header=True)
        table.add_column("Policy Groups")

        for i, group in enumerate(self.groups):
            name = group.name
            if i == self.selected_group_index:
                style = "bold white on blue"
                text = Text(f"{name}", style=style)
                if self.focus_on_groups:
                    text.append(" ◀", style="bold yellow")
            else:
                text = Text(f"{name}")
            table.add_row(text)

        return Panel(table, title="Groups")

    def create_proxies_panel(self) -> Panel:
        """Create a panel for proxies in the selected group."""
        if not self.current_group:
            return Panel("No policy group selected", title="Proxies")

        group_name = self.current_group.name
        group_delay_cache = self.delay_cache.get(group_name, {})

        return create_policy_group_table(
            group_data=self.current_group,
            delay_cache=group_delay_cache,
            selected_index=self.selected_proxy_index
            if not self.focus_on_groups
            else -1,
        )

    async def load_groups(self) -> None:
        """Load all policy groups."""
        try:
            data = await self.api_client.get_groups()
            self.groups = data.proxies
            if self.groups:
                # Load the first group
                await self.load_selected_group()
                self.status_message = "Policy groups loaded"
            else:
                self.status_message = "No policy groups available"
        except Exception as e:
            self.status_message = f"Error loading policy groups: {str(e)}"

    async def load_selected_group(self) -> None:
        """Load the selected policy group."""
        if not self.groups or self.selected_group_index >= len(self.groups):
            return

        try:
            group_name = self.groups[self.selected_group_index].name
            self.status_message = f"Loading group: {group_name}..."

            data = await self.api_client.get_group(group_name)
            self.current_group = data

            # Find the index of the currently selected proxy
            current = data.now
            proxies = data.all
            if current in proxies:
                self.selected_proxy_index = proxies.index(current)

            self.status_message = f"Group loaded: {group_name}"
        except Exception as e:
            self.status_message = f"Error loading group details: {str(e)}"

    async def select_proxy(self, group_name: str, proxy_name: str) -> None:
        """Select a proxy for the current group."""
        try:
            self.status_message = f"Selecting proxy: {proxy_name}..."
            await self.api_client.select_proxy(group_name, proxy_name)
            await self.load_selected_group()  # Reload to update current selection
            self.status_message = f"Selected proxy: {proxy_name}"
        except Exception as e:
            self.status_message = f"Error selecting proxy: {str(e)}"

    async def test_proxy_delay(self, proxy_name: str) -> None:
        """Test delay for a specific proxy."""
        if not self.current_group:
            return

        group_name = self.current_group.name

        try:
            self.status_message = f"Testing proxy delay: {proxy_name}..."
            result = await self.api_client.test_proxy_delay(proxy_name)

            # Extract delay information
            delay = result.delay

            # Update our delay cache
            if group_name not in self.delay_cache:
                self.delay_cache[group_name] = {}

            self.delay_cache[group_name][proxy_name] = delay

            self.status_message = f"Tested proxy: {proxy_name} - Delay: {delay}ms"
        except Exception as e:
            self.status_message = f"Error testing proxy delay: {str(e)}"

    async def test_group_delay(self, group_name: str) -> None:
        """Test delay for all proxies in a group."""
        if not self.current_group:
            return

        proxies = self.current_group.all
        if not proxies:
            return

        try:
            self.status_message = f"Testing all proxies in group: {group_name}..."

            # Initialize cache for this group if needed
            if group_name not in self.delay_cache:
                self.delay_cache[group_name] = {}

            results = await self.api_client.test_group_delay(group_name)
            for result in results:
                self.delay_cache[group_name][result.outbound] = result.delay

            self.status_message = f"Tested all proxies in group: {group_name}"
        except Exception as e:
            self.status_message = f"Error testing group delay: {str(e)}"

    async def run(self) -> None:
        """Run the policy group manager."""

        await self.load_groups()
        await self.app.run_async()
