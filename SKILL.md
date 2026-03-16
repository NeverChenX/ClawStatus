# ClawStatus

Real-time monitoring dashboard for OpenClaw ecosystem. Track devices, agents, sessions, cron jobs, models, and token usage.

## Features

- Real-time device monitoring with TCP probe detection
- Agent and subagent real-time tracking with execution status
- Session management with statistics and transcript history
- Cron job scheduling, execution monitoring, manual run, and deletion
- Model overview, configuration display, and model switching
- 15-day token usage analytics with archival data scanning
- System health monitoring and resource usage tracking
- Channel management and communication status
- Configurable refresh rates for dashboard updates
- English/Chinese bilingual interface with full i18n support
- Zero-polling architecture using inotify event monitoring
- Single-file deployment with minimal dependencies

## Installation

```bash
pip install --user -e .
```

## Usage

```bash
clawstatus --host 0.0.0.0 --port 8900 --no-debug
```
