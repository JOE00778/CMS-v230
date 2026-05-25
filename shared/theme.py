"""全局 UI 主题 · 极简版（只留字体 + 颜色 + 字号，造型还原 Streamlit 原生）

Boss 2026-05-25：撤掉 Modern White 造型（卡片/圆角/阴影/卡片底色/固定 1440 居中/
侧栏固定宽/按钮·输入·tab·alert 形状重绘/滚动条等），回到 Streamlit 原生结构。
仅保留：
- 字体：Inter + Noto Sans SC/JP 本地字体栈 + 图标字体保护
- 颜色：标题/正文/KPI/导航等文字色 + 强调色（按钮/勾选/tab 选中走 config.toml 原生 primaryColor）
- 字号：各处沿用此前调好的 px 值 + zoom 0.88 + config.toml baseFontSize=21
- 契约：.badge-* / html_table(.cms-table)

用法：每个 page 顶部、require_password() 之后调用一次 inject_theme()。
"""
from __future__ import annotations

import streamlit as st

_THEME_CSS = """
<style>
/* ===== 字体加载（本地打包·不连外网）=====
   Inter→拉丁/数字 · Noto Sans SC→中文 · Noto Sans JP→日文
   文件在 CMS/static/fonts/，经 Streamlit 静态服务暴露于 app/static/ */
@font-face { font-family:'Inter'; src:url('app/static/fonts/Inter-400.woff2') format('woff2'); font-weight:400; font-style:normal; font-display:swap; }
@font-face { font-family:'Inter'; src:url('app/static/fonts/Inter-500.woff2') format('woff2'); font-weight:500; font-style:normal; font-display:swap; }
@font-face { font-family:'Inter'; src:url('app/static/fonts/Inter-600.woff2') format('woff2'); font-weight:600; font-style:normal; font-display:swap; }
@font-face { font-family:'Inter'; src:url('app/static/fonts/Inter-700.woff2') format('woff2'); font-weight:700; font-style:normal; font-display:swap; }
@font-face { font-family:'Noto Sans SC'; src:url('app/static/fonts/NotoSansSC-400.woff2') format('woff2'); font-weight:400; font-style:normal; font-display:swap; }
@font-face { font-family:'Noto Sans SC'; src:url('app/static/fonts/NotoSansSC-700.woff2') format('woff2'); font-weight:700; font-style:normal; font-display:swap; }
@font-face { font-family:'Noto Sans JP'; src:url('app/static/fonts/NotoSansJP-400.woff2') format('woff2'); font-weight:400; font-style:normal; font-display:swap; }
@font-face { font-family:'Noto Sans JP'; src:url('app/static/fonts/NotoSansJP-700.woff2') format('woff2'); font-weight:700; font-style:normal; font-display:swap; }

/* ===== 字体栈（按字符自动选字：拉丁→Inter，中文→Noto SC，日文→Noto JP，缺则落系统）
   关键：只设 html/body + 显式文字元素，靠继承传递；绝不用 [class*=st-] 大锤，
   否则会盖掉 Streamlit 自带 Material Symbols 图标(class=st-emotion-cache-*)，露出 keyboard_double_arrow 等文本 */
html, body,
[data-testid="stPageLink"] a, [data-testid="stPageLink"] a p, [data-testid="stPageLink"] a span,
[data-baseweb="tab"], [data-baseweb="select"] div, [data-baseweb="select"] span,
[data-baseweb="popover"] li, [data-baseweb="menu"] li, [data-baseweb="input"] input,
button, textarea, select {
  font-family: 'Inter','Noto Sans SC','Noto Sans JP','PingFang SC','Hiragino Sans','Microsoft YaHei','Yu Gothic',system-ui,-apple-system,sans-serif !important;
}
/* 图标字体保护：真正的 Material Symbols 图标(markdown :material/...:)保留自身字体（backstop）*/
[class*="material-symbols"], [data-testid="stIconMaterial"], .material-icons {
  font-family: 'Material Symbols Rounded','Material Symbols Outlined','Material Symbols Sharp','Material Icons' !important;
}
/* 基础字号走 .streamlit/config.toml 的 [theme] baseFontSize=21（原生·喂给 dataframe 网格一致）。
   字体渲染平滑保留。 */
html, body {
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

/* ===== 颜色 + 字号（造型已还原 Streamlit 原生：卡片/圆角/阴影/边框/自定义间距全部移除）===== */

/* 标题：颜色 + 字号（字重保留；间距/字间距还原默认）*/
h1, h2, h3, h4 { color: #0F172A !important; font-weight: 700 !important; }
h1 { font-size: 36px !important; }
h2 { font-size: 30px !important; }
h3 { font-size: 24px !important; }
h4 { font-size: 20px !important; font-weight: 600 !important; }
/* 正文次级灰 */
p, span, label, div { color: #475569 !important; }

/* KPI：颜色 + 字号（卡片造型移除；保留数字不截断 + 等宽数字）*/
[data-testid="stMetricLabel"],
[data-testid="stMetricLabel"] p,
[data-testid="stMetricLabel"] div {
  color: #64748B !important;
  font-size: 13px !important;
  font-weight: 500 !important;
}
[data-testid="stMetricValue"] {
  color: #0F172A !important;
  font-size: 28px !important;
  font-weight: 700 !important;
  line-height: 1.25 !important;
  white-space: normal !important;
  overflow: visible !important;
  font-variant-numeric: tabular-nums !important;
  font-feature-settings: "tnum" 1 !important;
}
[data-testid="stMetricValue"] > div {
  white-space: normal !important;
  overflow: visible !important;
  text-overflow: clip !important;
}
[data-testid="stMetricDelta"] {
  font-size: 13px !important;
  font-weight: 600 !important;
  font-variant-numeric: tabular-nums !important;
}

/* 图表内文字字号（颜色/造型走原生）*/
[data-testid="stVegaLiteChart"] text,
[data-testid="stPlotlyChart"] text {
  font-family: inherit !important;
  font-size: 14px !important;
}

/* tab 字号（选中色走 config.toml 原生 primaryColor）*/
[data-baseweb="tab"] {
  font-size: 14px !important;
  font-weight: 500 !important;
}

/* 按钮字号（底色/形状走原生；primary 底色=config primaryColor）*/
[data-testid="stButton"] > button,
[data-testid="stDownloadButton"] > button,
[data-testid="stFormSubmitButton"] > button {
  font-size: 14px !important;
}
/* primary 内层文字强制白字（否则被全局 p,span,div{color:#475569} 盖成深灰，落主色底上看不清）*/
[data-testid="stButton"] > button[kind="primary"] *,
[data-testid="stFormSubmitButton"] > button[kind="primary"] * {
  color: #FFFFFF !important;
}

/* 侧栏导航：颜色 + 字号（形状/底色还原原生）*/
[data-testid="stSidebar"] [data-testid="stPageLink"] a { color: #475569 !important; }
[data-testid="stSidebar"] [data-testid="stPageLink"] a:hover { color: #4F46E5 !important; }
[data-testid="stSidebar"] [data-testid="stPageLink"] a p,
[data-testid="stSidebar"] [data-testid="stPageLink"] a span {
  font-size: 16px !important;
  font-weight: 500 !important;
}

/* 表单文字字号 + 聚焦色（输入框/下拉形状还原原生）*/
[data-baseweb="select"] div,
[data-baseweb="select"] span,
[data-baseweb="popover"] li,
[data-baseweb="popover"] [role="option"],
[data-baseweb="menu"] li,
ul[role="listbox"] li,
[data-baseweb="input"] input,
[data-baseweb="textarea"] textarea {
  font-size: 14px !important;
}
[data-baseweb="input"] input::placeholder,
[data-baseweb="textarea"] textarea::placeholder {
  font-size: 14px !important;
}
[data-baseweb="input"]:focus-within input,
[data-baseweb="select"]:focus-within > div {
  border-color: #4F46E5 !important;
}

/* caption 字号 */
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p {
  font-size: 13px !important;
}

/* 进度条颜色 */
[data-testid="stProgress"] > div > div > div {
  background: linear-gradient(90deg, #4F46E5, #0EA5E9) !important;
}

/* ===== CMS 自定义类（强制契约 · 保留）===== */
.badge-A, .badge-B, .badge-C, .badge-NEW, .badge-RED {
  padding: 2px 10px !important;
  border-radius: 980px !important;
  font-size: 11px !important;
  font-weight: 600 !important;
  display: inline-block;
}
.badge-A { background: rgba(16, 185, 129, 0.1) !important; color: #10B981 !important; }
.badge-B { background: rgba(79, 70, 229, 0.1) !important; color: #4F46E5 !important; }
.badge-C { background: rgba(100, 116, 139, 0.1) !important; color: #64748B !important; }
.badge-NEW { background: rgba(245, 158, 11, 0.1) !important; color: #F59E0B !important; }
.badge-RED { background: rgba(239, 68, 68, 0.1) !important; color: #EF4444 !important; }

/* ===== HTML 满宽表格（html_table 助手 · 保留 · 列自动拉伸填满）===== */
.cms-table-wrap { width:100% !important; overflow-x:auto; border:1px solid #E2E8F0; border-radius:16px; background:#FFFFFF; }
.cms-table { width:100% !important; border-collapse:collapse; font-size:14px; table-layout:auto; }
.cms-table thead th { background:#F8FAFC; color:#64748B; font-weight:600; font-size:12px; text-align:left; padding:10px 14px; border-bottom:1px solid #E2E8F0; white-space:nowrap; }
.cms-table tbody td { padding:9px 14px; color:#0F172A; border-bottom:1px solid #F1F5F9; font-variant-numeric:tabular-nums; white-space:nowrap; }
.cms-table tbody tr:last-child td { border-bottom:none; }
.cms-table tbody tr:hover td { background:#F8FAFC; }
.cms-table td.num, .cms-table th.num { text-align:right; }

/* ===== Zoom（字号体系的一部分 · 保留）=====
   字号数值按 zoom 0.88 等比补偿过（Inter 视觉偏大 ~8-10%），删 zoom 会让全站字变大。 */
.stApp { zoom: 0.88 !important; }
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

    cols = list(df.columns)
    head = "".join(
        (f'<th class="num">{_html.escape(str(c))}</th>' if i >= num_from_col
         else f'<th>{_html.escape(str(c))}</th>')
        for i, c in enumerate(cols)
    )
    body = []
    for _, row in df.iterrows():
        tds = "".join(
            (f'<td class="num">{_html.escape(str(v))}</td>' if i >= num_from_col
             else f'<td>{_html.escape(str(v))}</td>')
            for i, v in enumerate(row)
        )
        body.append(f"<tr>{tds}</tr>")
    st.markdown(
        '<div class="cms-table-wrap"><table class="cms-table">'
        f'<thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>',
        unsafe_allow_html=True,
    )


__all__ = ["inject_theme", "html_table"]
