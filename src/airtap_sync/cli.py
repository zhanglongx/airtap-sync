"""Command line entry point.

M0 only provides the skeleton: argument parsing, logging and config loading.
Sub-commands are registered with their final names and options, and report
which milestone will implement them.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from airtap_sync import __version__, paths
from airtap_sync.config import Config, ConfigError, load_config
from airtap_sync.gitlab.client import GitLabClient, GitLabError
from airtap_sync.gitlab.credentials import default_token_store
from airtap_sync.gitlab.projects import current_user
from airtap_sync.logs import setup_logging
from airtap_sync.medium.volume import VolumeError, init_volume

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_IMPLEMENTED = 2

log = logging.getLogger(__name__)

Handler = Callable[[argparse.Namespace], int]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="airtap-sync",
        description="在两个物理隔离的网络之间，通过 U 盘同步目录和 Git 仓库。",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--config", type=Path, metavar="FILE", help="配置文件路径（默认为用户配置目录）"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    sub = parser.add_subparsers(dest="command", metavar="<命令>", required=True)

    def add(
        name: str,
        help_text: str,
        milestone: str,
        *,
        parent: argparse._SubParsersAction | None = None,
    ) -> argparse.ArgumentParser:
        target = parent or sub
        p = target.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(handler=_not_implemented(f"{name}", milestone))
        return p

    p = add("init-usb", "初始化 U 盘，生成 volume.toml 和 pair_id", "M1")
    p.add_argument("path", type=Path, help="U 盘根目录")
    p.add_argument("--pair-id", help="沿用已有的 pair_id（U 盘丢失后重新初始化时使用）")
    p.set_defaults(handler=_init_usb)

    p = add("sync", "执行一次同步", "M2")
    p.add_argument("--usb", type=Path, metavar="PATH", help="U 盘路径（默认自动检测）")
    p.add_argument("--dry-run", action="store_true", help="只显示将要执行的操作")
    p.add_argument("--only", choices=["files", "git"], help="只同步目录或只同步 Git")
    p.add_argument("--force-full", action="store_true", help="跳过目录同步的握手轮")
    p.add_argument(
        "--confirm-deletes", action="store_true", help="自动确认超过阈值的删除"
    )

    add("status", "显示待同步的变化、上次同步时间和冲突", "M2")

    p = add("relay", "跨网电脑模式：直接在两个 GitLab 之间同步 Git", "M4")
    p.add_argument("--dry-run", action="store_true", help="只显示将要执行的操作")
    p.add_argument(
        "--confirm-deletes", action="store_true", help="自动确认超过阈值的删除"
    )

    gitlab = sub.add_parser("gitlab", help="GitLab 访问设置")
    gitlab_sub = gitlab.add_subparsers(
        dest="gitlab_command", metavar="<子命令>", required=True
    )
    p = add("login", "把访问令牌保存到系统凭据库", "M2", parent=gitlab_sub)
    p.set_defaults(handler=_gitlab_login)

    git = sub.add_parser("git", help="Git 同步的辅助命令")
    git_sub = git.add_subparsers(dest="git_command", metavar="<子命令>", required=True)
    add("snapshot", "GitLab 备份前记录基准快照（A 端）", "M2", parent=git_sub)
    p = add("baseline", "GitLab 恢复后采用基准快照（B 端）", "M2", parent=git_sub)
    p.add_argument("file", type=Path, help="snapshot 生成的基准文件")
    p = add("warm", "预先克隆最近活跃的工程到缓存", "M2", parent=git_sub)
    p.add_argument("--days", type=int, default=90, help="最近多少天内有活动（默认 90）")
    add("reset", "清空本地 Git 状态和缓存，以便重新初始化", "M2", parent=git_sub)

    add("agent", "启动后台常驻程序（托盘图标）", "M5")

    p = add("autostart", "管理开机自启", "M5")
    p.add_argument("action", choices=["enable", "disable"])
    return parser


def _init_usb(args: argparse.Namespace) -> int:
    try:
        volume = init_volume(args.path, args.pair_id)
    except VolumeError as e:
        print(f"错误：{e}", file=sys.stderr)
        return EXIT_ERROR
    print(f"U 盘已初始化：{volume.dir}")
    print(f'pair_id = "{volume.pair_id}"')
    print("请把这个 pair_id 写入 A、B 两台电脑的 config.toml。")
    return EXIT_OK


def _gitlab_login(args: argparse.Namespace) -> int:
    config = load_effective_config(args.config)
    if config.gitlab is None:
        raise ConfigError("config.toml 中没有 [gitlab] 配置")
    url = config.gitlab.url
    token = getpass.getpass(f"请输入 {url} 的访问令牌（需要 api 权限）：").strip()
    if not token:
        print("未输入令牌", file=sys.stderr)
        return EXIT_ERROR
    try:
        user = current_user(GitLabClient(url, token))
    except GitLabError as e:
        print(f"令牌验证失败：{e}", file=sys.stderr)
        return EXIT_ERROR
    default_token_store().set(url, token)
    print(f"已保存令牌：用户 {user.username}")
    if not user.is_admin:
        print(
            "警告：该用户不是管理员，无法列出全部工程，也无法在个人命名空间下建工程",
            file=sys.stderr,
        )
    return EXIT_OK


def _not_implemented(command: str, milestone: str) -> Handler:
    def handler(args: argparse.Namespace) -> int:
        print(f"命令 {command} 尚未实现（计划在 {milestone} 完成）", file=sys.stderr)
        return EXIT_NOT_IMPLEMENTED

    return handler


def load_effective_config(path: Path | None) -> Config:
    """Load the config from ``path`` or the default per-user location."""
    return load_config(path or paths.default_config_file())


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(verbose=args.verbose)
    log.debug("airtap-sync %s, command=%s", __version__, args.command)
    try:
        return args.handler(args)
    except ConfigError as e:
        print(f"配置错误：{e}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("已取消", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
