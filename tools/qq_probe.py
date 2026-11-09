#!/usr/bin/env python
"""QQ NT 只读 UIA 结构探针（命令行入口）。

用法（在项目根目录）：
    .venv\\Scripts\\python.exe tools\\qq_probe.py                 # 默认脱敏；不启动/不切前台
    .venv\\Scripts\\python.exe tools\\qq_probe.py --no-write      # 不落盘
    .venv\\Scripts\\python.exe tools\\qq_probe.py --activate      # 授权后：短暂置前以获取完整 UIA 树
    .venv\\Scripts\\python.exe tools\\qq_probe.py --launch-qq     # 仅在 QQ 未运行时启动 QQ（默认不启动）
    .venv\\Scripts\\python.exe tools\\qq_probe.py --window-classic --title "测试会话"

安全说明：
- 默认只输出结构统计（类名/控件类型/名称长度/节点数/标题长度），不输出窗口标题、
  联系人名、消息正文；
- ``--include-names`` 会输出控件 Name 原文（可能包含联系人与消息内容），仅限 Lead
  明确授权的测试会话，必须同时加 ``--i-understand-names-are-private``；
- 不点击、不滚动、不碰剪贴板、不读取 QQ 本地数据、不发送任何消息。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from social_secretary.collectors.qq import probe as probe_module  # noqa: E402
from social_secretary.collectors.qq.errors import QqError, QqProfileError  # noqa: E402
from social_secretary.collectors.qq.profile import QqUiMode, load_profile  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qq_probe.py",
        description="QQ NT 只读 UIA 结构探针（默认完全脱敏；不点击/不滚动/不碰剪贴板）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python tools/qq_probe.py --no-write\n"
            "  python tools/qq_probe.py --activate --no-write\n"
            "  python tools/qq_probe.py --window-efficiency --title 测试会话 --no-write\n"
        ),
    )
    parser.add_argument("--json-out", metavar="PATH", default=None, help="输出 JSON 路径；传 - 输出到 stdout")
    parser.add_argument("--no-write", action="store_true", help="不写 JSON 文件（只打印摘要）")
    parser.add_argument(
        "--include-names",
        action="store_true",
        help="输出控件名称原文（可能含联系人与消息内容；仅限 Lead 授权的测试会话）",
    )
    parser.add_argument(
        "--i-understand-names-are-private",
        action="store_true",
        dest="names_ack",
        help="确认已知晓 --include-names 的隐私风险（与 --include-names 配对使用）",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--window-classic",
        action="store_const",
        const="classic",
        dest="mode_filter",
        help="按经典模式选择器分析窗口结构（无法识别 narrow-toggler 时使用）",
    )
    mode_group.add_argument(
        "--window-efficiency",
        action="store_const",
        const="efficiency",
        dest="mode_filter",
        help="按效率模式选择器分析窗口结构（聊天内嵌在主面板时使用）",
    )
    parser.add_argument("--title", default=None, help="只分析窗口标题包含该子串的窗口（不输出标题原文）")
    parser.add_argument(
        "--activate",
        action="store_true",
        help="授权后使用：短暂把 QQ 窗口置前以构建完整 UIA 树，探测结束后恢复原前台窗口",
    )
    parser.add_argument(
        "--launch-qq",
        action="store_true",
        help="QQ 未运行时启动 D:\\QQ.exe（默认不启动；会打开客户端）",
    )
    parser.add_argument("--launch-timeout", type=int, default=20000, metavar="MS", help="启动 QQ 等待进程出现的超时")
    parser.add_argument("--max-depth", type=int, default=24, help="UIA 树扫描最大深度（默认 24）")
    parser.add_argument("--max-nodes", type=int, default=6000, help="每个窗口最多扫描节点数（默认 6000）")
    parser.add_argument("--max-windows", type=int, default=40, help="最多分析窗口数（默认 40）")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="QQ 未运行/无窗口/无 UIA 树时返回退出码 1（默认只要探针正常执行就返回 0）",
    )
    return parser


def _print_summary(report: dict, *, out_path: Path | None, stream) -> None:  # noqa: ANN001
    status = report.get("status")
    aggregate = report.get("aggregate") or {}
    version = report.get("version_check") or {}
    windows = report.get("windows") or []
    print(f"[qq_probe] 状态: {status} — {report.get('status_detail', '')}", file=stream)
    print(
        f"[qq_probe] QQ 进程: {len(report.get('processes') or [])} 个；"
        f"窗口: {len(windows)} 个（UIA 树可读: {aggregate.get('uia_roots_ok', 0)}）；"
        f"节点总数: {aggregate.get('node_count', 0)}",
        file=stream,
    )
    if version:
        detected = version.get("detected") or "未检测到"
        compatible = version.get("compatible")
        flag = "未知" if compatible is None else ("兼容" if compatible else "不兼容⚠")
        print(
            f"[qq_probe] QQ 版本: {detected}（基线 {version.get('baseline')}，校准结论: {flag}）",
            file=stream,
        )
    if windows:
        modes = sorted({window.get("analysis_mode", "?") for window in windows})
        print(f"[qq_probe] 分析模式: {', '.join(modes)}", file=stream)
        features = aggregate
        print(
            "[qq_probe] 选择器命中: "
            f"header={features.get('header_count', 0)} msg_area={features.get('msg_area_count', 0)} "
            f"search_box={features.get('search_box_count', 0)} editor={features.get('editor_count', 0)} "
            f"bubbles(classic={features.get('bubbles_classic', 0)}, "
            f"efficiency={features.get('bubbles_efficiency', 0)}) "
            f"self(classic={features.get('self_bubbles_classic', 0)}, "
            f"efficiency={features.get('self_bubbles_efficiency', 0)}) "
            f"time_nodes={features.get('time_node_count', 0)} "
            f"sender_nodes={features.get('sender_node_count', 0)}",
            file=stream,
        )
        for index, window in enumerate(windows[:8], start=1):
            print(
                f"[qq_probe]   窗口#{index} role={window.get('role')} "
                f"class={window.get('class_name', '')!r} "
                f"title_len={window.get('title_length', 0)} size={window.get('width')}x{window.get('height')} "
                f"visible={window.get('visible')} minimized={window.get('minimized')} "
                f"mode={window.get('analysis_mode')} nodes={window.get('node_count', 0)}",
                file=stream,
            )
    print(
        f"[qq_probe] 安全: 未点击/未滚动/未碰剪贴板/未读消息内容；"
        f"names_included={report.get('safety', {}).get('names_included', False)}；"
        f"前台已恢复={report.get('safety', {}).get('foreground_restored', True)}",
        file=stream,
    )
    if report.get("notes"):
        print("[qq_probe] 限制:", file=stream)
        for note in report["notes"]:
            print(f"[qq_probe]   - {note}", file=stream)
    if out_path is not None:
        print(f"[qq_probe] JSON 已写入: {out_path}", file=stream)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.include_names and not args.names_ack:
        print(
            "[qq_probe] 拒绝执行：--include-names 会输出联系人名/消息正文等隐私内容，"
            "仅限 Lead 明确授权的测试会话；如确需使用，请同时加 "
            "--i-understand-names-are-private。",
            file=sys.stderr,
        )
        return 2
    if args.include_names:
        print(
            "[qq_probe] ⚠ 已启用 --include-names：输出将包含控件名称原文（可能含联系人与消息内容）。"
            "请确认这是 Lead 授权的测试会话，并妥善保管输出文件。",
            file=sys.stderr,
        )
    if args.activate:
        print(
            "[qq_probe] ⚠ --activate：将短暂把 QQ 窗口置前以构建完整 UIA 树；"
            "探测结束后会尝试恢复原前台窗口。探测期间请勿操作键盘鼠标。",
            file=sys.stderr,
        )
    if args.launch_qq:
        print(
            "[qq_probe] ⚠ --launch-qq：QQ 未运行时会启动 D:\\QQ.exe（默认不会启动）。",
            file=sys.stderr,
        )
    try:
        profile = load_profile()
    except QqProfileError as exc:
        print(f"[qq_probe] 配置错误: {exc.message}", file=sys.stderr)
        return 2
    backend = probe_module.make_backend()
    options = probe_module.ProbeOptions(
        include_names=args.include_names,
        mode_filter=QqUiMode(args.mode_filter) if args.mode_filter else None,
        title_filter=args.title,
        activate=args.activate,
        launch_qq=args.launch_qq,
        launch_timeout_ms=args.launch_timeout,
        max_depth=max(1, args.max_depth),
        max_nodes=max(1, args.max_nodes),
        max_windows=max(1, args.max_windows),
    )
    started = datetime.now().astimezone()
    try:
        report = probe_module.collect_probe(backend, profile, options, generated_at=started)
    except QqError as exc:
        print(f"[qq_probe] 探测失败: {exc.code}: {exc.message}", file=sys.stderr)
        return 2
    finally:
        try:
            backend.close()
        except Exception:  # pragma: no cover
            pass

    out_path: Path | None = None
    if args.json_out == "-":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif not args.no_write:
        out_path = Path(args.json_out) if args.json_out else probe_module.default_output_path(started)
        try:
            probe_module.write_report(report, out_path)
        except OSError as exc:
            print(f"[qq_probe] 写入 JSON 失败: {exc}", file=sys.stderr)
            return 2
    _print_summary(report, out_path=out_path, stream=sys.stderr if args.json_out == "-" else sys.stdout)
    if args.strict and report.get("status") not in ("ok", "ok_sparse_tree"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
