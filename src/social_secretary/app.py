"""程序入口：GUI 启动与无界面自检（安装包/CI 用 `--selftest`）。

用法：
    python -m social_secretary                 # 启动桌面程序
    python -m social_secretary --demo          # 启动并载入演示数据
    python -m social_secretary --selftest      # 无界面自检，输出 JSON，退出码 0=通过
    python -m social_secretary --selftest --data-dir D:\\tmp\\ss-data
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import APP_NAME, APP_VERSION, AppPaths


def _build_service(data_dir: str | None = None):
    """组装核心服务。延迟 import，保证 --selftest 不需要 Qt 也能诊断。"""
    from .core.service import SocialSecretaryService

    paths = AppPaths.create(Path(data_dir) if data_dir else None)
    return SocialSecretaryService(paths)


def run_selftest(data_dir: str | None = None) -> int:
    payload: dict = {
        "app": APP_NAME,
        "version": APP_VERSION,
        "ok": False,
        "checks": [],
    }
    code = 2
    try:
        paths = AppPaths.create(Path(data_dir) if data_dir else None)
        payload["data_dir"] = str(paths.data_dir)

        # 1) 核心服务初始化
        from .core.service import SocialSecretaryService

        service = SocialSecretaryService(paths)
        payload["checks"].append({"name": "service_init", "ok": True})

        # 2) 演示数据 → 摘要（离线、不联网）
        service.load_demo_data()
        summary = service.generate_daily_summary()
        payload["checks"].append(
            {
                "name": "demo_summary",
                "ok": True,
                "counts": dict(summary.counts),
                "todos": len(summary.todos),
                "messages": len(summary.important) + len(summary.normal) + len(summary.fyi),
            }
        )

        # 3) 存储统计
        stats = service.get_storage_stats()
        payload["checks"].append({"name": "storage", "ok": True, "stats": stats})

        # 4) UI 模块可导入（不创建窗口）
        import importlib

        importlib.import_module("social_secretary.ui")
        payload["checks"].append({"name": "ui_import", "ok": True})

        service.shutdown()
        payload["ok"] = True
        code = 0
    except Exception as exc:  # noqa: BLE001 - selftest 需要报告任何异常
        payload["error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return code


def run_gui(data_dir: str | None = None, demo: bool = False) -> int:
    """启动桌面界面（Tkinter 标准库，避免 Qt 与部分宿主机音频驱动的兼容性问题）。"""
    from .ui import run_ui

    service = _build_service(data_dir)
    if demo:
        service.load_demo_data()
    try:
        return run_ui(service)
    finally:
        service.shutdown()


def main(argv: list[str] | None = None) -> int:
    # 冻结版 QQ 采集 worker：父进程以 sys.executable + --collector-worker 启动本 exe，
    # 必须在 argparse 之前转发，否则会被当成非法参数退出 2。
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] == "--collector-worker":
        from .core.collector_worker import main as collector_worker_main

        return collector_worker_main(raw_argv)
    if raw_argv and raw_argv[0] in ("--install", "--installer", "--uninstall"):
        # 已安装程序通过 `SocialSecretary.exe --uninstall` 执行卸载；安装器 onefile 也复用同一入口。
        from .installer.wizard import main as installer_main

        return installer_main(raw_argv)
    if raw_argv and raw_argv[0] in (
        "--mail-selftest",
        "--gui-e2e",
        "--gui-e2e-verify",
        "--qq-selftest",
        "--qq-read-selftest",
        "--qq-scan-selftest",
        "--qq-quick-selftest",
        "--p2-enum-probe",
        "--p2-picker-selftest",
        "--p2-sideeffects-selftest",
        "--p1-real-ai-selftest",
        "--one-click-gui-selftest",
        "--uia-identity-probe",
        "--error-feedback-gui-selftest",
        "--p2-multi-selftest",
        "--import-selftest",
    ):
        # 正式程序验收模式（真实邮箱/QQ 只读/导入页/真实界面；证据脱敏）。仅供验收。
        from .acceptance import (
            run_gui_e2e,
            run_import_selftest,
            run_mail_selftest,
            run_p2_enum_probe,
            run_p2_multi_selftest,
            run_p2_picker_selftest,
            run_error_feedback_gui_selftest,
            run_uia_identity_probe,
            run_one_click_gui_selftest,
            run_p1_real_ai_selftest,
            run_p2_sideeffects_selftest,
            run_qq_quick_selftest,
            run_qq_read_selftest,
            run_qq_scan_selftest,
            run_qq_selftest,
        )

        mode = raw_argv[0]
        e2e_parser = argparse.ArgumentParser(prog="social-secretary-acceptance")
        e2e_parser.add_argument("--data-dir", default=None)
        e2e_parser.add_argument("--out-dir", default=None)
        e2e_parser.add_argument("--email", default=None)
        e2e_parser.add_argument("--titles-file", default=None)
        e2e_parser.add_argument("--title", default=None)
        e2e_parser.add_argument("--first", type=int, default=0)
        e2e_parser.add_argument("--wait-seconds", type=int, default=60)
        e2e_parser.add_argument("--limit", type=int, default=20)
        e2e_parser.add_argument("--no-restore", action="store_true")
        e2e_parser.add_argument("--ack-may-mark-read", action="store_true")
        e2e_args = e2e_parser.parse_args(raw_argv[1:])
        if mode == "--error-feedback-gui-selftest":
            return run_error_feedback_gui_selftest(
                e2e_args.out_dir,
                data_dir=e2e_args.data_dir,
            )
        if mode == "--uia-identity-probe":
            return run_uia_identity_probe(
                str(e2e_args.title or ""),
                e2e_args.out_dir,
            )
        if mode == "--one-click-gui-selftest":
            return run_one_click_gui_selftest(
                e2e_args.out_dir,
                data_dir=e2e_args.data_dir,
                email=e2e_args.email,
                titles_file=e2e_args.titles_file,
            )
        if mode == "--p1-real-ai-selftest":
            return run_p1_real_ai_selftest(
                e2e_args.out_dir,
                data_dir=e2e_args.data_dir,
            )
        if mode == "--p2-enum-probe":
            return run_p2_enum_probe(e2e_args.out_dir, include_names=False)
        if mode == "--p2-sideeffects-selftest":
            return run_p2_sideeffects_selftest(
                e2e_args.out_dir,
                data_dir=e2e_args.data_dir,
                first=e2e_args.first or 2,
            )
        if mode == "--p2-picker-selftest":
            return run_p2_picker_selftest(
                e2e_args.out_dir,
                data_dir=e2e_args.data_dir,
                first=e2e_args.first or 2,
            )
        if mode == "--p2-multi-selftest":
            return run_p2_multi_selftest(
                e2e_args.out_dir,
                titles_file=e2e_args.titles_file,
                data_dir=e2e_args.data_dir,
                limit=e2e_args.limit,
                restore=not e2e_args.no_restore,
                first_count=e2e_args.first,
                ack_may_mark_read=e2e_args.ack_may_mark_read,
            )
        if mode == "--mail-selftest":
            return run_mail_selftest(e2e_args.data_dir, e2e_args.out_dir, e2e_args.email)
        if mode == "--qq-selftest":
            return run_qq_selftest(e2e_args.out_dir, wait_seconds=e2e_args.wait_seconds)
        if mode == "--qq-read-selftest":
            return run_qq_read_selftest(
                e2e_args.out_dir,
                wait_seconds=e2e_args.wait_seconds,
                limit=e2e_args.limit,
            )
        if mode == "--qq-scan-selftest":
            return run_qq_scan_selftest(
                e2e_args.out_dir,
                wait_seconds=e2e_args.wait_seconds,
                limit=e2e_args.limit,
            )
        if mode == "--qq-quick-selftest":
            return run_qq_quick_selftest(e2e_args.out_dir, e2e_args.data_dir)
        if mode == "--import-selftest":
            return run_import_selftest(e2e_args.out_dir, e2e_args.data_dir)
        return run_gui_e2e(
            e2e_args.data_dir,
            e2e_args.out_dir,
            e2e_args.email,
            verify_only=(mode == "--gui-e2e-verify"),
        )

    parser = argparse.ArgumentParser(prog="social-secretary", description=f"{APP_NAME} v{APP_VERSION}")
    parser.add_argument("--selftest", action="store_true", help="无界面自检并输出 JSON")
    parser.add_argument("--demo", action="store_true", help="启动时载入演示数据")
    parser.add_argument("--data-dir", default=None, help="覆盖本地数据目录（测试/便携模式）")
    args = parser.parse_args(raw_argv)

    if args.selftest:
        return run_selftest(args.data_dir)
    return run_gui(args.data_dir, args.demo)


if __name__ == "__main__":
    raise SystemExit(main())
