"""全局 UI 主题 · Modern White Sales Dashboard

设计核心：
- 浅灰背景 (#F1F5F9) + 纯白卡片 (#FFFFFF) + 靛蓝主色 (#4F46E5)
- 16px 圆角，柔和阴影，1px hairline 边框 (#E2E8F0)
- Inter + Noto Sans SC/JP 字体栈（本地打包，不连外网），21px 基础字号
- 严格遵循 CMS 主题格式规范（L1 config.toml + L2 本文件），保留 .badge-* 与 .density-* 契约

视觉回滚（满意版基线：max-width 1440 + KPI 28px + 导航 16px + tab 500）：
保留 Inter/Noto 字体 + 图标保护 + tabular-nums + html_table 等正面改进。
zoom：满意版是 system-ui + 0.95；改用 Inter 后字体视觉偏大 ~8-10%，故 zoom 降至 0.88 做全局
等比补偿（0.95 × (1-0.08) ≈ 0.87 → 取 0.88），字号数值保持满意版原值，整体比例与满意版一致。

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
html, body {
  font-size: 21px;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

/* ===== App 背景 ===== */
[data-testid="stAppViewContainer"] {
  background: #F1F5F9 !important;
}
[data-testid="stHeader"] {
  background: transparent !important;
  border-bottom: none !important;
  height: auto !important;
}

/* ===== 标题与正文 ===== */
h1, h2, h3, h4 {
  color: #0F172A !important;
  font-weight: 700 !important;
  letter-spacing: -0.025em !important;
  margin-bottom: 0.5rem !important;
}
h1 { font-size: 36px !important; line-height: 1.15 !important; }
h2 { font-size: 30px !important; }
h3 { font-size: 24px !important; }
h4 { font-size: 20px !important; font-weight: 600 !important; }
p, span, label, div { color: #475569 !important; }

/* ===== KPI 卡片 (stMetric) ===== */
[data-testid="stMetric"] {
  background: #FFFFFF !important;
  border: 1px solid #E2E8F0 !important;
  border-radius: 16px !important;
  padding: 20px 24px !important;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05) !important;
  transition: transform 0.2s ease, box-shadow 0.2s ease;
}
[data-testid="stMetric"]:hover {
  transform: translateY(-2px) !important;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.08) !important;
  border-color: #4F46E5 !important;
}
[data-testid="stMetricLabel"],
[data-testid="stMetricLabel"] p,
[data-testid="stMetricLabel"] div {
  color: #64748B !important;
  font-size: 13px !important;
  font-weight: 500 !important;
  text-transform: none !important;
}
[data-testid="stMetricValue"] {
  color: #0F172A !important;
  font-size: 28px !important;
  font-weight: 700 !important;
  letter-spacing: -0.01em !important;
  white-space: normal !important;
  overflow: visible !important;
  line-height: 1.25 !important;
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

/* ===== Sidebar ===== */
[data-testid="stSidebar"] {
  background: #FFFFFF !important;
  border-right: 1px solid #E2E8F0 !important;
  box-shadow: 2px 0 10px rgba(0, 0, 0, 0.02) !important;
  min-width: 280px !important;
  max-width: 280px !important;
  width: 280px !important;
}
[data-testid="stSidebar"] h1, [data-testid="stSidebar"] h2,
[data-testid="stSidebar"] h3, [data-testid="stSidebar"] h4 {
  color: #64748B !important;
  font-size: 11px !important;
  font-weight: 600 !important;
  text-transform: uppercase !important;
  letter-spacing: 1.5px !important;
  margin-top: 1rem !important;
}

/* ===== Dataframe ===== */
[data-testid="stDataFrame"] {
  background: #FFFFFF !important;
  border: 1px solid #E2E8F0 !important;
  border-radius: 16px !important;
  overflow: hidden !important;
}

/* ===== 图表容器 ===== */
[data-testid="stVegaLiteChart"],
[data-testid="stPlotlyChart"],
.chart-container {
  background: #FFFFFF !important;
  padding: 20px !important;
  border-radius: 16px !important;
  border: 1px solid #E2E8F0 !important;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05) !important;
}
[data-testid="stVegaLiteChart"] text,
[data-testid="stPlotlyChart"] text {
  font-family: inherit !important;
  font-size: 14px !important;
}

/* ===== Tabs ===== */
[data-baseweb="tab-list"] {
  border-bottom: 1px solid #E2E8F0 !important;
  gap: 1rem !important;
}
[data-baseweb="tab"] {
  color: #94A3B8 !important;
  font-weight: 500 !important;
  font-size: 14px !important;
  padding: 0.5rem 0.75rem !important;
  background: transparent !important;
}
[data-baseweb="tab"][aria-selected="true"] {
  color: #4F46E5 !important;
  border-bottom: 2px solid #4F46E5 !important;
}
[data-baseweb="tab-highlight"] {
  background: transparent !important;
  height: 0 !important;
}

/* ===== Buttons ===== */
[data-testid="stButton"] > button,
[data-testid="stDownloadButton"] > button,
[data-testid="stFormSubmitButton"] > button {
  border-radius: 10px !important;
  font-weight: 500 !important;
  font-size: 14px !important;
  padding: 0.55rem 1.25rem !important;
  transition: all 0.2s ease;
}
/* Secondary / Outline */
[data-testid="stButton"] > button:not([kind="primary"]),
[data-testid="stDownloadButton"] > button:not([kind="primary"]),
[data-testid="stFormSubmitButton"] > button:not([kind="primary"]) {
  background: transparent !important;
  border: 1px solid #E2E8F0 !important;
  color: #475569 !important;
  box-shadow: none !important;
}
[data-testid="stButton"] > button:not([kind="primary"]):hover,
[data-testid="stDownloadButton"] > button:not([kind="primary"]):hover {
  border-color: #4F46E5 !important;
  color: #4F46E5 !important;
  background: rgba(79, 70, 229, 0.05) !important;
}
/* Primary */
[data-testid="stButton"] > button[kind="primary"],
[data-testid="stFormSubmitButton"] > button[kind="primary"] {
  background: #4F46E5 !important;
  border: 1px solid #4F46E5 !important;
  color: #FFFFFF !important;
  box-shadow: none !important;
}
[data-testid="stButton"] > button[kind="primary"]:hover {
  background: #4338CA !important;
  border-color: #4338CA !important;
  box-shadow: 0 4px 12px rgba(79, 70, 229, 0.3) !important;
}
/* primary 内层文字强制白字（否则被全局 `p,span,div{color:#475569}` 盖成深灰，落深靛蓝底上看不清）*/
[data-testid="stButton"] > button[kind="primary"] *,
[data-testid="stFormSubmitButton"] > button[kind="primary"] * {
  color: #FFFFFF !important;
}

/* ===== Navigation (stPageLink) ===== */
[data-testid="stSidebar"] [data-testid="stPageLink"] a {
  border-radius: 10px !important;
  padding: 10px 12px !important;
  margin: 4px 0 !important;
  height: 40px !important;
  display: flex !important;
  align-items: center !important;
  color: #475569 !important;
  background: transparent !important;
  transition: background 0.2s ease, color 0.2s ease;
}
[data-testid="stSidebar"] [data-testid="stPageLink"] a:hover {
  background: rgba(79, 70, 229, 0.06) !important;
  color: #4F46E5 !important;
}
[data-testid="stSidebar"] [data-testid="stPageLink"] a p,
[data-testid="stSidebar"] [data-testid="stPageLink"] a span {
  font-size: 16px !important;
  font-weight: 500 !important;
}
/* Active State */
[data-testid="stSidebar"] [data-testid="stPageLink-NavLink"][href=""],
[data-testid="stSidebar"] [data-testid="stPageLink-NavLink"][href$="/"]:not([href*="?"]) {
  background: rgba(79, 70, 229, 0.1) !important;
  color: #4F46E5 !important;
  border: 1px solid rgba(79, 70, 229, 0.2) !important;
  font-weight: 600 !important;
}
[data-testid="stSidebar"] [data-testid="stPageLink-NavLink"][href=""] *,
[data-testid="stSidebar"] [data-testid="stPageLink-NavLink"][href$="/"]:not([href*="?"]) * {
  color: #4F46E5 !important;
}

/* ===== Inputs / Selects ===== */
[data-baseweb="input"] input,
[data-baseweb="select"] > div,
[data-baseweb="textarea"] textarea {
  border-radius: 10px !important;
  border-color: #E2E8F0 !important;
  background: #F8FAFC !important;
}
[data-baseweb="input"]:focus-within input,
[data-baseweb="select"]:focus-within > div {
  border-color: #4F46E5 !important;
  box-shadow: 0 0 0 2px rgba(79, 70, 229, 0.15) !important;
}
/* 输入框/下拉框文字上下居中 */
[data-baseweb="select"] > div { display: flex !important; align-items: center !important; }
[data-baseweb="input"] { display: flex !important; align-items: center !important; }
[data-baseweb="input"] input { height: 100% !important; }
/* 表单文字统一 14px：下拉框显示值/菜单选项 + 输入框/文本域(含 placeholder) */
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

/* ===== 页面说明 caption（标题下灰字 · 全站 st.caption · 比默认小 1px）===== */
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p {
  font-size: 13px !important;
}

/* ===== Progress Bar ===== */
[data-testid="stProgress"] > div > div > div {
  background: linear-gradient(90deg, #4F46E5, #0EA5E9) !important;
}

/* ===== Radio / Checkbox ===== */
[data-baseweb="radio"] [data-checked="true"] {
  background-color: #4F46E5 !important;
  border-color: #4F46E5 !important;
}
[data-testid="stCheckbox"] [data-checked="true"] {
  background-color: #4F46E5 !important;
  border-color: #4F46E5 !important;
}

/* ===== Tags / Multiselect ===== */
[data-baseweb="tag"] {
  background-color: #F1F5F9 !important;
  color: #475569 !important;
  border: 1px solid #E2E8F0 !important;
  border-radius: 20px !important;
  padding: 2px 8px !important;
}

/* ===== Alert / Expander ===== */
[data-testid="stAlert"], [data-testid="stExpander"] {
  border-radius: 12px !important;
  border: 1px solid #E2E8F0 !important;
  background: #FFFFFF !important;
}

/* ===== CMS 自定义类（强制契约）===== */
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

.density-compact [data-testid="stDataFrame"] td,
.density-compact [data-testid="stDataFrame"] th { padding: 0.4rem 0.6rem !important; font-size: 12px !important; }
.density-comfy [data-testid="stDataFrame"] td,
.density-comfy [data-testid="stDataFrame"] th { padding: 1rem 1.2rem !important; font-size: 15px !important; }

/* ===== Main Container（固定 1440 · 居中）===== */
[data-testid="stMainBlockContainer"], .main .block-container {
  padding-top: 1.5rem !important;
  padding-bottom: 1.5rem !important;
  max-width: 1440px !important;
  margin-left: auto !important;
  margin-right: auto !important;
}

/* ===== Scrollbar ===== */
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: transparent !important; }
::-webkit-scrollbar-thumb { background: #CBD5E1 !important; border-radius: 20px !important; }
::-webkit-scrollbar-thumb:hover { background: #94A3B8 !important; }

/* ===== HTML 满宽表格（html_table 助手 · 列自动拉伸填满，右边缘与图表/页面对齐）===== */
.cms-table-wrap { width:100% !important; overflow-x:auto; border:1px solid #E2E8F0; border-radius:16px; background:#FFFFFF; }
.cms-table { width:100% !important; border-collapse:collapse; font-size:14px; table-layout:auto; }
.cms-table thead th { background:#F8FAFC; color:#64748B; font-weight:600; font-size:12px; text-align:left; padding:10px 14px; border-bottom:1px solid #E2E8F0; white-space:nowrap; }
.cms-table tbody td { padding:9px 14px; color:#0F172A; border-bottom:1px solid #F1F5F9; font-variant-numeric:tabular-nums; white-space:nowrap; }
.cms-table tbody tr:last-child td { border-bottom:none; }
.cms-table tbody tr:hover td { background:#F8FAFC; }
.cms-table td.num, .cms-table th.num { text-align:right; }

/* ===== Zoom =====
   满意版用 system-ui + zoom 0.95。改用 Inter 后字体视觉偏大 ~8-10%（x-height 更高、笔画更饱满），
   故 zoom 降至 0.88 做全局等比补偿（0.95 × (1-0.08) ≈ 0.87 → 取 0.88），
   所有字号数值保持满意版原值不变，整体比例关系一致。 */
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
