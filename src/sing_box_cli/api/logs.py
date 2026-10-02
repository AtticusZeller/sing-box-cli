import re
from contextlib import aclosing
from datetime import datetime

from rich import print
from rich.text import Text

from ..common import LogLevel
from .client import SingBoxAPIClient


async def get_logs(client: SingBoxAPIClient, log_level: LogLevel) -> None:
    # Define color mapping for log levels with brighter colors
    # Ref: https://rich.readthedocs.io/en/stable/appendix/colors.html
    log_level_colors = {
        "trace": "bright_black",
        "debug": "grey66",
        "info": "bright_cyan",
        "warning": "bright_yellow",
        "error": "bright_red",
        "fatal": "bright_magenta",
        "panic": "bright_magenta",
    }

    # Bright colors for IDs
    id_colors = [
        "bright_red",
        "bright_green",
        "bright_yellow",
        "bright_blue",
        "bright_magenta",
        "bright_cyan",
    ]

    # Dictionary to store ID to color mapping
    id_color_map = {}

    try:
        async with aclosing(client.log_stream(log_level.value.lower())) as stream:
            async for entry in stream:
                # Get current timestamp with automatic timezone offset
                now = datetime.now().astimezone()
                timestamp = now.strftime("%z %Y-%m-%d %H:%M:%S")

                # Get level color
                # Start building the output message
                message = Text()
                message.append(timestamp, style="bright_black")
                message.append(" ")
                message.append(
                    entry.type.upper(),
                    style=log_level_colors.get(entry.type.lower(), "bright_white"),
                )
                message.append(" ")

                # Process the payload
                payload = entry.payload

                # Find all ID patterns in the payload
                pattern = r"\[(\d+) (\d+ms)\]"
                last_end = 0

                for match in re.finditer(pattern, payload):
                    # Add text before the match
                    message.append(payload[last_end : match.start()])

                    # Extract ID and time part
                    log_id = match.group(1)
                    time_part = match.group(2)

                    # Assign a consistent color to this ID
                    if log_id not in id_color_map:
                        color_index = hash(log_id) % len(id_colors)
                        id_color_map[log_id] = id_colors[color_index]

                    id_color = id_color_map[log_id]

                    # Add the ID with coloring
                    message.append("[", style="default")
                    message.append(log_id, style=id_color)
                    message.append(f" {time_part}]", style="default")

                    last_end = match.end()

                # Add any remaining text
                if last_end < len(payload):
                    message.append(payload[last_end:])

                # Print the complete message
                print(message)
    except Exception as e:
        print(f"[red]Error:[/red] {e}")
        raise
