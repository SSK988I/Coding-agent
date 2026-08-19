"""CLI 参数解析。

使用 argparse 解析已支持的命令行参数。只有具备实际运行行为的选项才会显示
在帮助信息中。

"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

from coding_agent.core.config import APP_NAME, VERSION

# ─── Argument definitions ────────────────────────────────────────────────


@dataclass
class Args:
    """解析后的命令行参数。"""

    # 模型选择
    provider: str | None = None
    model: str | None = None
    api_key: str | None = None
    thinking: str | None = None

    # 系统提示词
    system_prompt: str | None = None
    append_system_prompt: list[str] | None = None

    # 会话管理
    continue_session: bool = False
    session: str | None = None  # --session <path|id>
    session_id: str | None = None  # --session-id <id>
    fork_session: str | None = None  # --fork <path|id>
    session_dir: str | None = None
    no_session: bool = False
    name: str | None = None

    # 工具管理
    tools: list[str] | None = None
    exclude_tools: list[str] | None = None
    no_tools: bool = False
    no_builtin_tools: bool = False

    no_context_files: bool = False

    # 技能加载
    skill_paths: list[str] | None = None  # --skill (repeatable)
    no_skills: bool = False

    # 提示词模板
    prompt_template_paths: list[str] | None = None  # --prompt-template (repeatable)
    no_prompts: bool = False

    # 运行模式
    print_mode: bool = False
    #: 非交互模式输出格式：``text`` 为最终回复，``json`` 为事件流。
    output_mode: str = "text"
    export_path: str | None = None
    list_models: str | None = None  # None=off, ""=all, "str"=search

    # 运行行为
    project_trust_override: bool | None = None  # None=default, True=approve, False=no-approve

    # 位置参数：初始消息和 @file 引用
    messages: list[str] = field(default_factory=list)
    file_args: list[str] = field(default_factory=list)  # from @file args

    # 解析过程中收集的诊断信息
    diagnostics: list[dict] = field(default_factory=list)

    @property
    def help(self) -> bool:
        return False  # argparse handles --help before we get here

    @property
    def version(self) -> bool:
        return False  # argparse handles --version before we get here


_VALID_THINKING_LEVELS = {"off", "minimal", "low", "medium", "high", "xhigh"}


class _ChineseHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """把 argparse 自动生成的用法标题统一为中文。"""

    def add_usage(
        self,
        usage: str | None,
        actions: list[argparse.Action],
        groups: list[argparse._MutuallyExclusiveGroup],
        prefix: str | None = None,
    ) -> None:
        super().add_usage(usage, actions, groups, prefix or "用法：")


def _validate_thinking(value: str) -> str:
    """校验并规范化 ``--thinking`` 参数。"""
    value = value.strip().lower()
    if value not in _VALID_THINKING_LEVELS:
        raise argparse.ArgumentTypeError(
            f"无效的思考级别：'{value}'。"
            f"可选值：{', '.join(sorted(_VALID_THINKING_LEVELS))}"
        )
    return value


def _parse_comma_list(value: str) -> list[str]:
    """解析逗号分隔的列表参数，并过滤空字符串。"""
    return [s.strip() for s in value.split(",") if s.strip()]


# ─── Parser construction ──────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """构建包含全部 CLI 选项的 argparse 解析器。

    返回配置完成的 ``ArgumentParser``；调用 ``parse_args()`` 可获得
    ``Args`` 数据类。
    """
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description=f"{APP_NAME} - 支持读取、命令执行、编辑和写入工具的 AI 编程助手",
        formatter_class=_ChineseHelpFormatter,
        epilog=_build_epilog(),
        add_help=False,
    )

    general_group = parser.add_argument_group("通用选项")
    general_group.add_argument(
        "-h", "--help",
        action="help",
        help="显示帮助信息并退出",
    )

    # ── Model selection ──────────────────────────────────────────────────
    model_group = parser.add_argument_group("模型选择")
    model_group.add_argument(
        "--provider",
        help="Provider 名称（deepseek、zhipu）；智谱别名：glm、zai、zai-coding-cn",
    )
    model_group.add_argument(
        "--model",
        help="模型匹配模式或 ID；支持 'provider/id' 和可选的 ':thinking' 简写",
    )
    model_group.add_argument(
        "--api-key",
        help="API Key（默认读取 Provider 对应的环境变量）",
    )
    model_group.add_argument(
        "--thinking",
        type=_validate_thinking,
        help=f"思考级别：{', '.join(sorted(_VALID_THINKING_LEVELS))}",
    )
    # ── System prompt ────────────────────────────────────────────────────
    prompt_group = parser.add_argument_group("系统提示词")
    prompt_group.add_argument(
        "--system-prompt",
        help="替换系统提示词",
    )
    prompt_group.add_argument(
        "--append-system-prompt",
        action="append",
        help="在系统提示词后追加文本（可多次使用）",
    )

    # ── Session management ───────────────────────────────────────────────
    session_group = parser.add_argument_group("会话管理")
    session_selector = session_group.add_mutually_exclusive_group()
    session_selector.add_argument(
        "-c", "--continue",
        dest="continue_session",
        action="store_true",
        help="继续最近一次会话",
    )
    session_selector.add_argument(
        "--session",
        help="使用指定会话文件或部分 UUID",
    )
    session_selector.add_argument(
        "--session-id",
        help="使用准确的项目会话 ID；不存在时自动创建",
    )
    session_selector.add_argument(
        "--fork",
        dest="fork_session",
        help="从指定会话创建新分支会话",
    )
    session_group.add_argument(
        "--session-dir",
        help="会话存储目录",
    )
    session_selector.add_argument(
        "--no-session",
        action="store_true",
        help="不保存会话（临时会话）",
    )
    session_group.add_argument(
        "-n", "--name",
        help="设置会话显示名称",
    )

    # ── Tool management ──────────────────────────────────────────────────
    tool_group = parser.add_argument_group("工具管理")
    tool_group.add_argument(
        "-t", "--tools",
        type=_parse_comma_list,
        help="以逗号分隔的工具启用列表",
    )
    tool_group.add_argument(
        "-xt", "--exclude-tools",
        type=_parse_comma_list,
        help="以逗号分隔的工具禁用列表",
    )
    tool_group.add_argument(
        "-nt", "--no-tools",
        action="store_true",
        help="禁用全部工具（内置和自定义）",
    )
    tool_group.add_argument(
        "-nbt", "--no-builtin-tools",
        action="store_true",
        help="禁用内置工具",
    )

    # ── Project context ──────────────────────────────────────────────────
    context_group = parser.add_argument_group("项目上下文")
    context_group.add_argument(
        "-nc", "--no-context-files", action="store_true",
        help="禁用 AGENTS.md 和 CLAUDE.md 自动发现",
    )
    context_group.add_argument(
        "--skill",
        dest="skill_paths",
        action="append",
        help="额外加载的技能文件或目录（可多次使用）",
    )
    context_group.add_argument(
        "-ns", "--no-skills", action="store_true",
        help="禁用技能发现与加载",
    )
    context_group.add_argument(
        "--prompt-template",
        dest="prompt_template_paths",
        action="append",
        help="额外加载的提示词模板文件或目录（可多次使用）",
    )
    context_group.add_argument(
        "-np", "--no-prompts", action="store_true",
        help="禁用提示词模板发现与加载",
    )

    # ── Mode flags ───────────────────────────────────────────────────────
    mode_group = parser.add_argument_group("运行模式")
    mode_group.add_argument(
        "-p", "--print",
        dest="print_mode",
        action="store_true",
        help="非交互模式：处理提示词后退出",
    )
    mode_group.add_argument(
        "--mode",
        choices=["text", "json"],
        default="text",
        help="非交互输出格式：'text' 为最终回复，'json' 为事件流（每行一个 JSON 对象）",
    )
    mode_group.add_argument(
        "--export",
        dest="export_path",
        help="将会话文件导出为 HTML 后退出",
    )
    mode_group.add_argument(
        "--list-models",
        nargs="?",
        const="",
        help="列出可用模型（可选模糊搜索）",
    )

    # ── Behavior ─────────────────────────────────────────────────────────
    behavior_group = parser.add_argument_group("运行行为")
    trust_group = behavior_group.add_mutually_exclusive_group()
    trust_group.add_argument(
        "-a", "--approve",
        dest="project_trust_override",
        action="store_const",
        const=True,
        default=None,
        help="本次运行信任项目本地指令文件",
    )
    trust_group.add_argument(
        "-na", "--no-approve",
        dest="project_trust_override",
        action="store_const",
        const=False,
        help="本次运行忽略项目上下文文件",
    )

    # ── Version ──────────────────────────────────────────────────────────
    general_group.add_argument(
        "-v", "--version",
        action="version",
        version=f"{APP_NAME} {VERSION}",
        help="显示版本信息并退出",
    )

    # ── Positional: initial messages and @file references ────────────────
    positional_group = parser.add_argument_group("位置参数")
    positional_group.add_argument(
        "args",
        nargs="*",
        help="初始消息和（或）@file 文件引用",
    )

    return parser


def _build_epilog() -> str:
    """生成包含示例的帮助尾注。"""
    return f"""示例：
  # 交互模式
  {APP_NAME}

  # 带初始提示词的交互模式
  {APP_NAME} "列出 src/ 下的所有 Python 文件"

  # 在初始消息中附加文件
  {APP_NAME} @prompt.md "这个文件有什么作用？"

  # 非交互模式
  {APP_NAME} -p "列出 src/ 下的所有 Python 文件"

  # 继续上一次会话
  {APP_NAME} --continue "我们刚才讨论了什么？"

  # 使用其他模型
  {APP_NAME} --model deepseek-v4-pro "帮我重构这段代码"

  # 使用智谱 GLM Coding Plan（GLM-5.2 等）
  {APP_NAME} --provider zhipu --model glm-5.2 "重构这个函数"

  # 列出可用模型
  {APP_NAME} --list-models

  # 将会话导出为 HTML
  {APP_NAME} --export session.jsonl

环境变量：
  DEEPSEEK_API_KEY             DeepSeek API Key
  ZAI_CODING_CN_API_KEY        智谱 GLM Coding Plan API Key（也支持 ZHIPU_API_KEY、GLM_API_KEY）
  CODING_AGENT_HOME            配置目录（默认：~/.coding-agent）
"""


# ─── Parse entry point ────────────────────────────────────────────────────


def parse_args(argv: list[str] | None = None) -> Args:
    """把 CLI 参数解析为 ``Args`` 数据类。

    Args:
        argv: 参数列表，默认为 ``sys.argv[1:]``。

    Returns:
        解析后的 ``Args``，其中包含警告和错误诊断。
    """
    parser = build_parser()
    ns = parser.parse_args(argv)

    args = Args()
    _apply_namespace(args, ns)
    return args


def _apply_namespace(args: Args, ns: argparse.Namespace) -> None:
    """把 argparse 命名空间映射到 ``Args`` 数据类。

    处理 ``--approve``/``--no-approve`` 冲突、@file 拆分，以及用户以字符串
    形式传入的逗号列表参数。
    """
    # 模型
    args.provider = ns.provider
    args.model = ns.model
    args.api_key = ns.api_key
    args.thinking = ns.thinking

    # 系统提示词
    args.system_prompt = ns.system_prompt
    args.append_system_prompt = list(ns.append_system_prompt) if ns.append_system_prompt else None

    # 会话
    args.continue_session = ns.continue_session
    args.session = ns.session
    args.session_id = ns.session_id
    args.fork_session = ns.fork_session
    args.session_dir = ns.session_dir
    args.no_session = ns.no_session
    args.name = ns.name

    # 工具
    args.tools = ns.tools
    args.exclude_tools = ns.exclude_tools
    args.no_tools = ns.no_tools
    args.no_builtin_tools = ns.no_builtin_tools

    args.no_context_files = ns.no_context_files
    args.skill_paths = list(ns.skill_paths) if ns.skill_paths else None
    args.no_skills = ns.no_skills
    args.prompt_template_paths = list(ns.prompt_template_paths) if ns.prompt_template_paths else None
    args.no_prompts = ns.no_prompts

    # 运行模式
    args.print_mode = ns.print_mode
    args.output_mode = ns.mode
    args.export_path = ns.export_path
    args.list_models = ns.list_models  # None=off, ""=all, "str"=search

    # 项目信任：--approve 为 True，--no-approve 为 False，均未提供时为 None
    #（使用默认上下文加载行为）。argparse 会拒绝同时提供两个参数。
    args.project_trust_override = ns.project_trust_override

    # 把位置参数拆分为 @file 引用和普通消息。
    for arg in ns.args or []:
        if arg.startswith("@"):
            args.file_args.append(arg[1:])  # strip @ prefix
        else:
            args.messages.append(arg)


# ─── Convenience ──────────────────────────────────────────────────────────


def resolve_app_mode(args: Args) -> str:
    """根据解析结果和终端状态确定运行模式。

    返回 ``interactive`` 或 ``print``。

    使用 ``--print``、``--mode json``（机器可读输出），或 stdin/stdout 不是
    TTY（管道输入输出）时强制使用 ``print``，否则使用 ``interactive``。
    """
    if args.print_mode:
        return "print"
    if getattr(args, "output_mode", "text") == "json":
        return "print"
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return "print"
    return "interactive"
