"""Host-OS-dependent behavior for Cleave.

Single source of truth for the few places Cleave's behavior diverges by host
operating system. Selected by GlobalConfig.os ("unix" | "mac").
"""

from __future__ import annotations

SUPPORTED_OS: tuple[str, ...] = ("unix", "mac")


def terminal_launch_command(os_name: str) -> str:
    """Command *prefix* that opens an interactive terminal running the script
    appended after it. The caller shlex-quotes and appends the script path.

    - mac:  `open -a Terminal '<script>'`  — runs the executable script in
            Terminal.app.
    - unix: `gnome-terminal -- bash -c '<script>'` — unchanged Linux default.
    """
    if os_name == "mac":
        return "open -a Terminal"
    return "gnome-terminal -- bash -c"
