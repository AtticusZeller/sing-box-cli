import re
from contextlib import aclosing
from datetime import datetime

from rich import print
from rich.text import Text

from ..common import LogLevel
from .client import SingBoxAPIClient


async def get_logs(client: SingBoxAPIClient, log_level: LogLevel) -> None:
    log_level_colors = {
        "trace": "bright_black",
        "debug": "grey66",
        "info": "bright_cyan",
        "warning": "bright_yellow",
        "error": "bright_red",
        "fatal": "bright_magenta",
        "panic": "bright_magenta",
    }

    id_colors = [
        "bright_red",
        "bright_green",
        "bright_yellow",
        "bright_blue",
        "bright_magenta",
        "bright_cyan",
    ]

    id_color_map = {}

    async with aclosing(client.log_stream(log_level.value.lower())) as stream:
        async for entry in stream:
            now = datetime.now().astimezone()
            timestamp = now.strftime("%z %Y-%m-%d %H:%M:%S")

            message = Text()
            message.append(timestamp, style="bright_black")
            message.append(" ")
            message.append(
                entry.type.upper(),
                style=log_level_colors.get(entry.type.lower(), "bright_white"),
            )
            message.append(" ")

            payload = entry.payload

            pattern = r"\[(\d+) (\d+ms)\]"
            last_end = 0

            for match in re.finditer(pattern, payload):
                message.append(payload[last_end : match.start()])

                log_id = match.group(1)
                time_part = match.group(2)

                if log_id not in id_color_map:
                    color_index = hash(log_id) % len(id_colors)
                    id_color_map[log_id] = id_colors[color_index]

                id_color = id_color_map[log_id]

                message.append("[", style="default")
                message.append(log_id, style=id_color)
                message.append(f" {time_part}]", style="default")

                last_end = match.end()

            if last_end < len(payload):
                message.append(payload[last_end:])

            print(message)
