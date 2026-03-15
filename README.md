# ClawStatus

A standalone OpenClaw status dashboard for monitoring devices, sessions, models, and token usage.

## Features

- Single-file status dashboard
- Suitable for local or LAN access
- Displays device status, session statistics, model info, and 15-day token usage overview

## Installation

```bash
pip install --user -e .
```

## Usage

```bash
clawstatus --host 0.0.0.0 --port 8900 --no-debug
```

## Service Management (systemd --user)

```bash
systemctl --user restart clawstatus.service
systemctl --user status clawstatus.service
```
