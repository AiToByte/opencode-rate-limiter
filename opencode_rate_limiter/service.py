"""Service-file generators: systemd unit, launchd plist, Windows task XML."""

from __future__ import annotations

import shutil
from pathlib import Path

from platformdirs import user_config_dir


def _default_config_path_str() -> str:
    return str(Path(user_config_dir("opencode-rate-limiter")) / "config.toml")


def _resolve_binary() -> str:

    return shutil.which("opencode-rate-limiter") or "opencode-rate-limiter"


def generate_systemd_unit() -> str:
    """Generate a systemd user unit file for the daemon"""
    return f"""\
[Unit]
Description=OpenCode Rate Limiter Daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
ExecStart={_resolve_binary()} daemon
Restart=on-failure
RestartSec=10
Environment=OPENCODE_RATE_LIMITER_CONFIG={_default_config_path_str()}
# 可选: 限制资源
MemoryMax=100M
CPUQuota=10%

[Install]
WantedBy=default.target
"""


def generate_launchd_plist() -> str:
    """Generate a macOS launchd plist for the daemon"""
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.opencode.ratelimiter</string>
    <key>ProgramArguments</key>
    <array>
        <string>{_resolve_binary()}</string>
        <string>daemon</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
        <key>Crashed</key>
        <true/>
    </dict>
    <key>StandardOutPath</key>
    <string>/tmp/opencode-rate-limiter.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/opencode-rate-limiter.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>OPENCODE_RATE_LIMITER_CONFIG</key>
        <string>{_default_config_path_str()}</string>
    </dict>
</dict>
</plist>
"""


def generate_task_xml() -> str:
    """Generate a Windows Task Scheduler XML for the daemon"""
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>OpenCode Rate Limiter Daemon</Description>
    <Author>opencode-rate-limiter</Author>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
  </Settings>
  <Actions>
    <Exec>
      <Command>{_resolve_binary()}</Command>
      <Arguments>daemon</Arguments>
      <WorkingDirectory>%USERPROFILE%</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""
