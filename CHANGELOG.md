# CHANGELOG

## 0.1.0

- 新增 `scripts/gitlab_stats.py`：只读统计 GitLab 工程代码规模，用于确定 Git 缓存大小
- M0 工程骨架：`pyproject.toml`、配置加载与校验、平台路径、日志、进度事件、命令行骨架（各命令暂未实现）、GitHub Actions CI、pytest 测试
- M1 U 盘介质层：`init-usb`（支持 `--pair-id` 重新初始化）、outbox 原子写入与 `commit.json` 校验、传输配额、大文件切片、暂存与拼接
- M2a-1 GitLab 接入：REST 客户端（分页、重试、错误提示）、工程清单与排除规则、令牌存储（keyring，不可用时退回 600 权限文件）、`gitlab login` 命令
