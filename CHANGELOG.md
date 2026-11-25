# CHANGELOG

## 0.1.0

- 新增 `scripts/gitlab_stats.py`：只读统计 GitLab 工程代码规模，用于确定 Git 缓存大小
- M0 工程骨架：`pyproject.toml`、配置加载与校验、平台路径、日志、进度事件、命令行骨架（各命令暂未实现）、GitHub Actions CI、pytest 测试
- M1 U 盘介质层：`init-usb`（支持 `--pair-id` 重新初始化）、outbox 原子写入与 `commit.json` 校验、传输配额、大文件切片、暂存与拼接
- M2a-1 GitLab 接入：REST 客户端（分页、重试、错误提示）、工程清单与排除规则、令牌存储（keyring，不可用时退回 600 权限文件）、`gitlab login` 命令
- M2a-2 Git 缓存：git 命令封装（令牌只通过环境变量注入且限定在 GitLab 地址）、并发 `ls-remote` 变化检测、按需克隆与 LRU 缓存、`git warm` 命令
- M2b ref 同步：分支与 tag 的三方合并（只快进、分叉停放到 `airtap/conflict/`、删除传播并备份、tag 冲突不覆盖）、增量 bundle 生成/校验/导入、推送（ci.skip、被拒的 ref 单独重试）、分支过滤；含双端随机模拟测试
- M2c 工程层：工程清单（manifest）、工程三方合并（对方新建则本端自动建群组和工程；删除、改名、转移只报告不处理）、改名识别、默认分支与归档状态复制、Git 同步状态库、`git snapshot` / `git baseline` / `git reset` 命令
- M2d Git 完整同步流程：`sync` 命令（`--usb`、`--dry-run`、`--confirm-deletes`）串起读取对方数据、工程与分支合并、导入、导出 bundle（增量、全量回退、按配额分批、断点续传）、防误删阈值确认、同步报告（写入 U 盘和本地）
- 新增 `gitlab check`：只读检查令牌、管理员权限、GitLab 版本、工程列表耗时、通过 HTTP 读取代码、LFS 接口、external_url；新增 LFS Batch 客户端雏形
- 新增 `config.example.toml` 配置示例，并加测试保证示例始终可解析
