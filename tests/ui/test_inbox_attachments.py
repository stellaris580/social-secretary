"""P0-21：收件箱详情展示附件文件名（只名称，不下载）。"""

from __future__ import annotations


def _inbox(window):
    return window.pages["inbox"]


def _select_real_row(page) -> int:
    rows = page.rows()
    return next(index for index, row in enumerate(rows) if row["values"][0] == "真实")


def test_inbox_detail_shows_attachment_names_without_download(service, window):
    service.seed_inbox_mail(
        subject="周末活动确认（含附件）",
        text="请查看附件中的场地与人数。",
        attachment_names=("活动方案.pdf", "人数统计.xlsx"),
        is_demo=False,
    )
    page = _inbox(window)
    page.refresh()
    assert page.select_row(_select_real_row(page)) is True
    detail = page.detail_content()
    assert "来源：" in detail
    assert "附件：活动方案.pdf、人数统计.xlsx" in detail
    assert "共 2 个" in detail
    assert "程序未下载/不保存附件内容" in detail


def test_inbox_detail_without_attachments_has_no_attachment_line(service, window):
    service.seed_inbox_mail(subject="无附件邮件", text="正文", attachment_names=())
    page = _inbox(window)
    page.refresh()
    assert page.select_row(_select_real_row(page)) is True
    assert "附件：" not in page.detail_content()


def test_inbox_attachment_names_truncated_and_rows_values_order_unchanged(service, window):
    long_name = "很长的附件文件名" + "x" * 60 + ".pdf"
    service.seed_inbox_mail(
        subject="多个附件",
        text="正文",
        attachment_names=(long_name, "b.docx", "c.png"),
    )
    page = _inbox(window)
    page.refresh()
    rows = page.rows()
    assert rows, "应有邮件行"
    real_row = next(row for row in rows if row["values"][0] == "真实")
    # 固定钩子 values 顺序必须保持 (标记, 发件人, 主题, 日期, 正文片段)
    assert len(real_row["values"]) == 5
    assert real_row["values"][0] == "真实"
    assert real_row["values"][2] == "多个附件"
    assert real_row["has_attachment"] is True

    assert page.select_row(rows.index(real_row)) is True
    detail = page.detail_content()
    assert "附件：" in detail
    assert "b.docx" in detail and "c.png" in detail
    assert "共 3 个" in detail
    assert "…" in detail  # 超长文件名截断展示
