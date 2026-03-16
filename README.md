# ClawStatus

A real-time monitoring dashboard for [OpenClaw](https://github.com/anthropics/OpenClaw) ecosystem projects. Track devices, agents, sessions, cron jobs, models, and token usage across your entire OpenClaw deployment.

## Features

- **Real-time Device Monitoring** - Track online/offline status and health of all connected devices with TCP probe detection
- **Agent & Subagent Tracking** - Real-time visibility into running agents, subagents, and their execution status
- **Session Management** - View active sessions with detailed statistics and transcript history
- **Cron Job Management** - Monitor scheduled tasks, view execution history, run jobs manually, and delete crons
- **Model Configuration** - Display available models, switch models for agents/crons, and view configurations
- **Token Usage Analytics** - 15-day token consumption trends with daily breakdown and archival data scanning
- **System Health Monitoring** - Real-time system resource usage, memory status, and OpenClaw ecosystem health
- **Channel Management** - View and manage communication channels with real-time status
- **Configurable Refresh Rates** - Adjust dashboard refresh speed based on your needs
- **Multi-language Support** - English and Chinese interface with full i18n support
- **Single-file Deployment** - No complex dependencies, easy to deploy anywhere
- **Zero-polling Architecture** - Uses inotify event monitoring on Linux for efficient resource usage

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

## License

MIT
