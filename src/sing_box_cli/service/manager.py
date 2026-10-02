import subprocess
from pathlib import Path

from ..config.config import ConfigHandler, run_args, run_cmd


class ServiceManager:
    def __init__(self, config: ConfigHandler) -> None:
        self.config = config

    def create_service(self) -> None:
        raise NotImplementedError()

    def check_service(self) -> bool:
        raise NotImplementedError()

    def start(self) -> None:
        raise NotImplementedError()

    def stop(self) -> None:
        raise NotImplementedError()

    def restart(self) -> None:
        raise NotImplementedError()

    def status(self) -> str:
        raise NotImplementedError()

    def disable(self) -> None:
        raise NotImplementedError()

    def version(self) -> str:
        raise NotImplementedError()


class WindowsServiceManager(ServiceManager):
    """Windows SCM service managed by the bundled native Go helper."""

    def __init__(self, config: ConfigHandler) -> None:
        super().__init__(config)
        self.service_name = "sing-box-service"

    @property
    def helper_bin(self) -> str:
        # Match sing-box-bin: Windows always ships the amd64 executable.
        binary = Path(__file__).parents[1] / "bin" / "sbc-service-windows-amd64.exe"
        if not binary.is_file():
            raise FileNotFoundError(
                f"Windows service helper not found: {binary}. Reinstall sing-box-cli from a built wheel."
            )
        return str(binary)

    def create_service(self) -> None:
        self.check_service()  # Fail on SCM access errors before attempting installation.
        subprocess.run(
            [
                self.helper_bin,
                "install",
                self.service_name,
                "--work-dir",
                str(self.config.config_dir),
                "--",
                *run_args(self.config),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )

    def check_service(self) -> bool:
        result = subprocess.run(
            [self.helper_bin, "exists", self.service_name],
            capture_output=True,
            text=True,
        )
        if result.returncode == 1060:  # ERROR_SERVICE_DOES_NOT_EXIST
            return False
        result.check_returncode()
        return True

    def _service_state(self) -> str:
        result = subprocess.run(
            [self.helper_bin, "status", self.service_name],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def start(self) -> None:
        if self.check_service() and self._service_state() == "SERVICE_RUNNING":
            return
        subprocess.run(
            [self.helper_bin, "start", self.service_name],
            check=True,
            stdout=subprocess.DEVNULL,
        )

    def stop(self) -> None:
        if not self.check_service() or self._service_state() == "SERVICE_STOPPED":
            return
        subprocess.run(
            [self.helper_bin, "stop", self.service_name],
            check=True,
            stdout=subprocess.DEVNULL,
        )

    def restart(self) -> None:
        subprocess.run(
            [self.helper_bin, "restart", self.service_name],
            check=True,
            stdout=subprocess.DEVNULL,
        )

    def status(self) -> str:
        if not self.check_service():
            return "Service not installed"
        return self._service_state().replace("_", " ").title()

    def disable(self) -> None:
        if not self.check_service():
            return
        subprocess.run(
            [self.helper_bin, "remove", self.service_name],
            check=True,
            stdout=subprocess.DEVNULL,
        )

    def version(self) -> str:
        result = subprocess.run([self.config.bin_path, "version"], capture_output=True)
        return result.stdout.decode("utf-8").strip()


class LinuxServiceManager(ServiceManager):
    def __init__(self, config: ConfigHandler) -> None:
        super().__init__(config)
        self.service_name = "sing-box"
        self.service_file = Path("/etc/systemd/system/sing-box.service")

    def create_service(self) -> None:
        """systemctl list-units | grep -i network
        Refs:
            1. https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html#Type
            2. https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#Scheduling
        """
        service_content = f"""
[Unit]
Description=sing-box service
Documentation=https://sing-box.sagernet.org
After=network-online.target nss-lookup.target

[Service]
Type=exec
LimitNOFILE=infinity
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW CAP_NET_BIND_SERVICE CAP_SYS_TIME CAP_SYS_PTRACE CAP_DAC_READ_SEARCH CAP_DAC_OVERRIDE
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW CAP_NET_BIND_SERVICE CAP_SYS_TIME CAP_SYS_PTRACE CAP_DAC_READ_SEARCH CAP_DAC_OVERRIDE

# restart
Restart=on-failure
RestartSec=5
StartLimitInterval=60
StartLimitBurst=3
# start commands
ExecStart={run_cmd(self.config)}
ExecReload=/bin/kill -HUP $MAINPID

[Install]
WantedBy=multi-user.target
"""
        self.service_file.write_text(service_content)
        subprocess.run(["systemctl", "daemon-reload"])
        subprocess.run(["systemctl", "enable", self.service_name])

    def check_service(self) -> bool:
        return self.service_file.exists()

    def start(self) -> None:
        subprocess.run(["systemctl", "start", self.service_name])

    def stop(self) -> None:
        subprocess.run(["systemctl", "stop", self.service_name])

    def restart(self) -> None:
        subprocess.run(["systemctl", "restart", self.service_name])

    def status(self) -> str:
        try:
            subprocess.check_call(["systemctl", "is-active", self.service_name])
            return "Running"
        except Exception:
            return "Stopped"

    def disable(self) -> None:
        self.stop()
        subprocess.run(["systemctl", "disable", self.service_name])
        if self.service_file.exists():
            self.service_file.unlink()

    def version(self) -> str:
        result = subprocess.run([self.config.bin_path, "version"], capture_output=True)
        return result.stdout.decode("utf-8").strip()


def create_service(
    config: ConfigHandler,
) -> WindowsServiceManager | LinuxServiceManager:
    return (
        WindowsServiceManager(config)
        if config.is_windows
        else LinuxServiceManager(config)
    )
