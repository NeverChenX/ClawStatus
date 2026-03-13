# ClawStatus

独立的 OpenClaw 状态看板项目，用于查看设备、会话、模型与 Token 消耗概览。

## 特性

- 单文件启动的状态看板
- 适合本机或局域网访问
- 提供设备状态、会话统计、模型与近 15 天 Token 消耗概览

## 安装

```bash
pip install --user -e .
```

## 运行

```bash
clawstatus --host 0.0.0.0 --port 8900 --no-debug
```

## 服务管理（systemd --user）

```bash
systemctl --user restart clawstatus.service
systemctl --user status clawstatus.service
```
