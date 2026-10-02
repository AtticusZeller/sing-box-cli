import asyncio

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import (
    FormattedTextControl,
    HSplit,
    Layout as PTLayout,
    Window,
)
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, TextArea
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .client import ConnectionData, ConnectionInfo, SingBoxAPIClient
from .monitor import (
    format_bytes,
    format_chain,
    format_duration,
    format_rule,
    sort_connections,
)
from .widget import create_header_panel, render_rich_to_prompt_toolkit


def create_connections_table(
    connections_data: ConnectionData,
    total: int,
    highlight_index: int = -1,
    max_display: int = 20,
) -> Panel:
    """
    Create a panel displaying active connections.

    Args:
        connections_data: Connections data from the API
        total: Total number of connections
        highlight_index: Index of the row to highlight (-1 for none)
        max_display: Maximum number of connections to display

    Returns:
        Rich Panel containing the connections information
    """
    if len(connections_data.connections) == 0:
        return Panel("No connections data available", title="Active Connections")

    connections = connections_data.connections

    if not connections:
        return Panel("No active connections", title="Active Connections")

    # Create table to display connections
    table = Table(
        title=f"Active Connections ({total})",
        expand=True,
        pad_edge=False,  # Reduce padding
    )
    table.add_column("Host", style="cyan", no_wrap=True, max_width=20)
    table.add_column("Rule", style="bright_green", no_wrap=True, max_width=15)
    table.add_column("Chain", style="yellow", no_wrap=True, max_width=20)
    table.add_column("Network", justify="center", style="green")
    table.add_column("↑", justify="right", style="bright_blue")
    table.add_column("↓", justify="right", style="magenta")
    table.add_column("Duration", justify="right", style="green")

    # Sort connections by time
    sorted_connections = sort_connections(connections)

    # Show top connections (limit to avoid overwhelming the display)
    for idx, conn in enumerate(sorted_connections[:max_display]):
        # Extract data
        metadata = conn.metadata
        host = metadata.host or metadata.destinationIP

        network = metadata.network.upper()
        upload = format_bytes(conn.upload)
        download = format_bytes(conn.download)

        # Extract rule information
        rule_display = format_rule(conn.rule)

        # Calculate duration
        duration_str = format_duration(conn.start)

        # Format chains (usually shows proxy names)
        chain_str = format_chain(conn.chains)

        # Determine if this row should be highlighted
        if idx == highlight_index:
            # Create style for highlighted row
            style = "on blue"
            table.add_row(
                Text(host, style=style),
                Text(rule_display, style=style),
                Text(chain_str, style=style),
                Text(network, style=style),
                Text(upload, style=style),
                Text(download, style=style),
                Text(duration_str, style=style),
            )
        else:
            table.add_row(
                host, rule_display, chain_str, network, upload, download, duration_str
            )

    # Add summary information
    total_upload = format_bytes(connections_data.uploadTotal)
    total_download = format_bytes(connections_data.downloadTotal)

    summary = f"Total Upload: {total_upload} | Total Download: {total_download}"
    if len(connections) > max_display:
        summary += f" | Showing {max_display} of {len(connections)} connections"

    # Return as a panel
    return Panel(
        Group(table, Text(summary, justify="center")), title="Connection Details"
    )


class ConnectionsManager:
    """Interactive connections manager for Sing-Box connections."""

    def __init__(self, api_client: SingBoxAPIClient) -> None:
        """Initialize the connections manager."""
        self.api_client = api_client
        self.connections: list[ConnectionInfo] = []
        self.filtered_connections: list[ConnectionInfo] = []
        self.page = 0
        self.page_size = 20
        self.selected_index = 0
        self.filter_text = ""
        self.status_message = "Loading connections..."
        self.total_upload = 0
        self.total_download = 0

        # Setup components
        self.kb = KeyBindings()
        self.setup_keybindings()

        # Create application
        self.layout = self.create_layout()

        self.app: Application[None] = Application(
            layout=self.layout,
            key_bindings=self.kb,
            mouse_support=True,
            full_screen=True,
            style=self.get_style(),
            refresh_interval=0.5,
        )

    def get_style(self) -> Style:
        """Define the application style."""
        return Style.from_dict({"status-bar": "bg:#333333 #ffffff"})

    @property
    def selected_connection(self) -> ConnectionInfo:
        """Get the currently selected connection."""
        return self.filtered_connections[
            self.page * self.page_size + self.selected_index
        ].model_copy()

    def setup_keybindings(self) -> None:
        """Setup keyboard shortcuts."""

        @self.kb.add("c-c")
        @self.kb.add("c-q")
        @self.kb.add(Keys.SIGINT)
        def _(event) -> None:  # type: ignore[no-untyped-def]  # noqa: ARG001
            """Quit the application."""
            event.app.exit()

        @self.kb.add("c-d")
        def _(event) -> None:  # type: ignore[no-untyped-def]  # noqa: ARG001
            """Close the connection."""
            if self.filtered_connections:
                conn_id = self.selected_connection.id
                event.app.create_background_task(self.close_connection(str(conn_id)))

        @self.kb.add("c-x", eager=True)
        def _(event) -> None:  # type: ignore[no-untyped-def]  # noqa: ARG001
            """Close all connections."""
            event.app.create_background_task(self.close_all_connections())

        @self.kb.add("down")
        def _(event) -> None:  # type: ignore[no-untyped-def]  # noqa: ARG001
            """Move selection down."""
            if self.selected_index < min(
                self.page_size - 1,
                len(self.filtered_connections) - 1 - self.page * self.page_size,
            ):
                self.selected_index += 1

        @self.kb.add("up")
        def _(event) -> None:  # type: ignore[no-untyped-def]  # noqa: ARG001
            """Move selection up."""
            if self.selected_index > 0:
                self.selected_index -= 1

        @self.kb.add("right")
        def _(event) -> None:  # type: ignore[no-untyped-def]  # noqa: ARG001
            """Next page."""
            if (self.page + 1) * self.page_size < len(self.filtered_connections):
                self.page += 1
                self.selected_index = 0
            self.status_message = self.get_filter_state()

        @self.kb.add("left")
        def _(event) -> None:  # type: ignore[no-untyped-def]  # noqa: ARG001
            """Previous page."""
            if self.page > 0:
                self.page -= 1
                self.selected_index = 0
            self.status_message = self.get_filter_state()

        @self.kb.add("home")
        def _(event) -> None:  # type: ignore[no-untyped-def]  # noqa: ARG001
            """Go to first page."""
            self.page = 0
            self.selected_index = 0
            self.status_message = self.get_filter_state()

        @self.kb.add("end")
        def _(event) -> None:  # type: ignore[no-untyped-def]  # noqa: ARG001
            """Go to last page."""
            self.page = max(0, (len(self.filtered_connections) - 1) // self.page_size)
            self.selected_index = 0
            self.status_message = self.get_filter_state()

    def apply_filter(self, buffer: Buffer) -> None:
        """Apply search filter to connections."""
        self.filter_text = buffer.text.lower()
        self.filter_connections()
        self.page = 0
        self.selected_index = 0
        self.status_message = self.get_filter_state()

    def filter_connections(self) -> None:
        """Filter connections based on search text."""
        if not self.filter_text:
            self.filtered_connections = self.connections
            return

        # Filter connections that match the search text in any field
        self.filtered_connections = []
        for conn in self.connections:
            # Check host
            host = conn.metadata.host.lower()
            if self.filter_text in host:
                self.filtered_connections.append(conn)
                continue

            # Check chains
            chains = format_chain(conn.chains).lower()
            if self.filter_text in chains:
                self.filtered_connections.append(conn)
                continue

            # Check network
            network = conn.metadata.network.lower()
            if self.filter_text in network:
                self.filtered_connections.append(conn)
                continue

    def create_layout(self) -> PTLayout:
        """Create the application layout."""
        # Title bar

        title_panel = create_header_panel("Sing-Box Connections Manager")
        title_window = Window(
            height=3,
            content=FormattedTextControl(render_rich_to_prompt_toolkit(title_panel)),
        )

        # Filter input
        filter_input_window = Frame(
            body=self.get_filter_input(), style="class:filter-input"
        )

        # Connections list display
        self.connections_window = Window(
            content=FormattedTextControl(self.get_connections_display),
            always_hide_cursor=False,
            wrap_lines=False,
            allow_scroll_beyond_bottom=True,
        )

        # Status bar
        status_window = Window(
            content=FormattedTextControl(self.get_status_display),
            height=1,
            style="class:status-bar",
        )

        # Help bar
        help_window = Window(
            height=1,
            content=FormattedTextControl(
                lambda: [
                    (
                        "class:status-bar",
                        " Ctrl+C/Ctrl+Q: Quit, Ctrl+D: Close, Ctrl+X: Close All, "
                        " ↑/↓: Move,  ←/→: Page, "
                        " Home/End: First/Last Page, "
                        " Input: Search",
                    )
                ]
            ),
            style="class:status-bar",
        )

        # Main layout
        root_container = HSplit(
            [
                title_window,
                filter_input_window,
                self.connections_window,
                status_window,
                help_window,
            ]
        )

        return PTLayout(root_container, focused_element=self.connections_window)

    def get_filter_state(self) -> str:
        """Get the filter state display."""
        current_range = f"{self.page * self.page_size + 1}-{min((self.page + 1) * self.page_size, len(self.filtered_connections))}"
        return (
            f"Showing {current_range} of {len(self.filtered_connections)} connections "
        )

    def get_filter_input(self) -> TextArea:
        filter_input = TextArea(
            height=1,
            prompt=" Search: ",
            style="class:search-field",
            multiline=False,
            wrap_lines=False,
        )
        filter_input.buffer.on_text_changed += self.apply_filter
        return filter_input

    def get_status_display(self) -> FormattedText:
        """Get the status bar display."""
        return FormattedText([("class:status-bar", f" Status: {self.status_message}")])

    def get_connections_display(self) -> FormattedText:
        """Get the formatted text for connections display."""
        if not self.filtered_connections:
            if self.filter_text:
                return FormattedText([("", "No connections match your filter.")])
            return FormattedText([("", "No active connections.")])

        # Prepare data for current page
        start_idx = self.page * self.page_size
        end_idx = min(start_idx + self.page_size, len(self.filtered_connections))

        # Create connections data for the current page
        page_connections = ConnectionData(
            connections=self.filtered_connections[start_idx:end_idx],
            uploadTotal=self.total_upload,
            downloadTotal=self.total_download,
        )

        # Create rich panel with highlighted row
        panel = create_connections_table(
            connections_data=page_connections,
            total=len(self.filtered_connections),
            highlight_index=self.selected_index,
            max_display=self.page_size,
        )

        # Convert to prompt_toolkit format
        return render_rich_to_prompt_toolkit(panel)

    async def close_connection(self, conn_id: str) -> None:
        """Close a specific connection."""
        cnnt_info = f"Connection {conn_id=}"
        try:
            await self.api_client.close_connection(conn_id)
            host = self.selected_connection.metadata.host
            cnnt_info = f"Connection {host=} {conn_id=}"
            self.status_message = f"{cnnt_info} closed successfully"
            # Refresh connections after closing one
            await self.refresh_connections()
        except Exception as e:
            self.status_message = f"Error closing {cnnt_info}: {str(e)}"

    async def close_all_connections(self) -> None:
        """Close all connections."""
        try:
            await self.api_client.close_all_connections()
            self.status_message = "All connections closed successfully"
            # Refresh connections after closing all
            await self.refresh_connections()
        except Exception as e:
            self.status_message = f"Error closing all connections: {str(e)}"

    async def refresh_connections(self) -> None:
        """Refresh the connections data."""
        try:
            data = await self.api_client.get_connections()
            # Sort connections by time and host
            self.connections = sort_connections(data.connections)
            self.total_upload = data.uploadTotal
            self.total_download = data.downloadTotal
            self.filter_connections()  # Apply current filter
        except Exception as e:
            self.status_message = f"Error refreshing connections: {str(e)}"
            # clean up connections
            self.connections = []
            self.filtered_connections = []
            self.total_upload = 0
            self.total_download = 0
            raise

    async def run(self) -> None:
        """Run the interactive manager."""
        self.running = True
        refresh_task = None
        try:
            # Initial load of connections
            await self.refresh_connections()
            self.status_message = self.get_filter_state()
            refresh_task = asyncio.create_task(self.periodic_refresh())
            await self.app.run_async()
        finally:
            self.running = False
            if refresh_task is not None:
                refresh_task.cancel()
                try:
                    await refresh_task
                except asyncio.CancelledError:
                    pass

    async def periodic_refresh(self, interval: float = 0.5) -> None:
        """Periodically refresh connections."""
        while self.running:
            try:
                await asyncio.sleep(interval)
                await self.refresh_connections()
            except asyncio.CancelledError:
                break
