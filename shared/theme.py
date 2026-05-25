"""全局 UI 主题 · 已撤销（Boss 2026-05-25：全部还原到最原始 = 完全默认 Streamlit）

不再注入任何字体/颜色/字号/缩放/造型 CSS。config.toml 的 [theme] 段也已移除。
唯一保留：html_table 助手的表格样式（.cms-table）——否则 page05 等的满宽 HTML
表格会退化成无样式裸表。inject_theme() 仅注入这一小段表格样式。

用法：每个 page 顶部、require_password() 之后调用一次 inject_theme()（现为近乎 no-op）。
"""
from __future__ import annotations

import streamlit as st

_THEME_CSS = """
<style>
/* 页面内容区固定最大宽 1600 居中（Boss 2026-05-25）。其余保持 Streamlit 默认。 */
[data-testid="stMainBlockContainer"], .main .block-container {
  max-width: 1600px !important;
  margin-left: auto !important;
  margin-right: auto !important;
}

/* 完全默认 Streamlit 主题（无自定义字体/颜色/字号/缩放）。
   仅保留 html_table 助手的表格样式——否则满宽 HTML 表格会变成无样式裸表。 */
.cms-table-wrap { width:100% !important; overflow-x:auto; border:1px solid #E2E8F0; border-radius:8px; background:#FFFFFF; margin-bottom:2rem; }
.cms-table { width:100% !important; border-collapse:collapse; table-layout:auto; }
.cms-table thead th { background:#F8FAFC; color:#64748B; font-weight:600; text-align:left; padding:10px 14px; border-bottom:1px solid #E2E8F0; white-space:nowrap; }
.cms-table tbody td { padding:9px 14px; border-bottom:1px solid #F1F5F9; font-variant-numeric:tabular-nums; white-space:nowrap; }
.cms-table tbody tr:last-child td { border-bottom:none; }
.cms-table tbody tr:hover td { background:#F8FAFC; }
.cms-table td.num, .cms-table th.num { text-align:right; }

/* KPI 指标卡片化（Boss 2026-05-25）·组件级选择器，仅命中 st.metric，不影响 dataframe */
[data-testid="stMetric"] {
  background:#F8FAFC; border:1px solid #E2E8F0; border-radius:10px;
  padding:14px 16px; box-shadow:0 1px 2px rgba(15,23,42,.04);
}
/* 卡片数字字号缩小（Boss 2026-05-26）·默认约 2.25rem → 1.6rem
   用 !important + 连子元素一起命中，避免被 Streamlit emotion 类的高优先级盖掉 */
[data-testid="stMetricValue"],
[data-testid="stMetricValue"] > div { font-size:1.6rem !important; }

/* 分隔符 st.divider（=<hr>）上下间距压到 0（Boss 2026-05-26） */
hr { margin-top:0 !important; margin-bottom:0 !important; }
</style>
"""


def inject_theme() -> None:
    """注入全局 Modern White 风格主题 CSS（每页 require_password 之后调用一次）。"""
    st.markdown(_THEME_CSS, unsafe_allow_html=True)


def html_table(df, *, num_from_col: int = 1) -> None:
    """渲染满宽 HTML 表格（列自动拉伸填满容器，右边缘与图表/页面对齐）。

    - 值需已格式化为字符串（如 _disp() 的输出）。
    - 第 num_from_col 列起按数字列右对齐（默认第 1 列起；第 0 列通常是名称/日期，左对齐）。
    - 适合中小表（≤ 数百行）；超大表（千行级）仍用 st.dataframe 以保性能。
    """
    import html as _html
    import re as _re

    def _fmt(v) -> str:
        """单元格 escape 后，给涨跌 (+..%/pp) 上绿、(-..%/pp) 上红（Boss 2026-05-25）。"""
        s = _html.escape(str(v))
        s = _re.sub(r'(\(\+[^)]*\))', r'<span style="color:#16A34A;font-weight:600">\1</span>', s)
        s = _re.sub(r'(\(-[^)]*\))', r'<span style="color:#DC2626;font-weight:600">\1</span>', s)
        return s

    cols = list(df.columns)
    head = "".join(
        (f'<th class="num">{_html.escape(str(c))}</th>' if i >= num_from_col
         else f'<th>{_html.escape(str(c))}</th>')
        for i, c in enumerate(cols)
    )
    body = []
    for _, row in df.iterrows():
        tds = "".join(
            (f'<td class="num">{_fmt(v)}</td>' if i >= num_from_col
             else f'<td>{_fmt(v)}</td>')
            for i, v in enumerate(row)
        )
        body.append(f"<tr>{tds}</tr>")
    st.markdown(
        '<div class="cms-table-wrap"><table class="cms-table">'
        f'<thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>',
        unsafe_allow_html=True,
    )


__all__ = ["inject_theme", "html_table"]
