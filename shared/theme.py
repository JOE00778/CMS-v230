"""全局 UI 主题 · Neumorphism (Soft UI) + SMIKIE 红强调（2026-05-21 最小改造）

设计核心：
- system-ui 字体栈（按系统语言自动选中日字形），负字距大标题
- #E0E5EC 冷灰同色背景，元素与背景同材质，靠双向阴影定义凸起/凹陷（无边框）
- 双向阴影令牌（:root --neu-out / --neu-in 等）：左上白光 + 右下冷灰
- 卡片/按钮/dataframe/expander 凸起（--neu-out）；输入框/alert 凹陷井（--neu-in）
- 单一品牌红 #d6000f（SMIKIE）保留作强调色（primary CTA / 选中 / 告警）
- 16-24px 圆角；按钮 pill / 980px-radius
- 限制：st.dataframe 内部 canvas / 图表内部为框架限制，仅做外壳 neumorphic

用法：每个 page 顶部调用 inject_theme()，建议放在 require_password() 之后。
提供 .badge-A/.badge-B/.badge-C/.badge-NEW/.badge-RED 类供 page 使用。
"""
from __future__ import annotations

import streamlit as st

_THEME_CSS = """
<style>
/* ===== Neumorphism (Soft UI) 令牌 · 2026-05-21 最小改造 ·
   同色冷灰背景 + 双向阴影(左上白光/右下冷灰)定义凸起/凹陷 · 保留 SMIKIE 红做强调 */
:root {
    --neu-bg: #E0E5EC;
    --neu-out: 9px 9px 16px rgba(163,177,198,0.6), -9px -9px 16px rgba(255,255,255,0.5);
    --neu-out-hover: 12px 12px 20px rgba(163,177,198,0.7), -12px -12px 20px rgba(255,255,255,0.6);
    --neu-out-sm: 5px 5px 10px rgba(163,177,198,0.6), -5px -5px 10px rgba(255,255,255,0.5);
    --neu-in: inset 6px 6px 10px rgba(163,177,198,0.6), inset -6px -6px 10px rgba(255,255,255,0.5);
    --neu-in-deep: inset 10px 10px 20px rgba(163,177,198,0.7), inset -10px -10px 20px rgba(255,255,255,0.6);
}

/* ===== 系统字体（Boss 2026-05-21 选：按系统语言自动选 CJK 字形）=====
   system-ui: Mac→苹方/ヒラギノ · Windows(元川)→微软雅黑/游ゴシック，OS 按 locale 自适应中日汉字字形
   不再加载网络字体（去掉旧 Inter / Noto Sans JP @import）*/

/* 字体：只设 html/body，让继承传递；不覆盖 Streamlit 内部
   为图标 span 设置的 Material Symbols Outlined 字体（否则 ligature 失效，
   会露出 keyboard_arrow_down / keyboard_arrow_right 等原始文本） */
html, body {
    font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI',
                 'Helvetica Neue', Arial, sans-serif;
    font-size: 21px;  /* 基础字体（rem 連動で全体拡大）*/
    background: #E0E5EC;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
}

/* ===== App 背景（neumorphic 同色冷灰）===== */
[data-testid="stAppViewContainer"] {
    background: var(--neu-bg);
}
/* 顶部 header 完全透明，不再遮住 h1 标题 */
[data-testid="stHeader"] {
    background: transparent;
    border-bottom: none;
    height: auto;
}

/* ===== 标题：Apple 大字号 + 负字距 ===== */
h1 {
    font-size: 40px !important;
    font-weight: 600 !important;
    letter-spacing: -0.022em !important;
    color: #1d1d1d !important;
    line-height: 1.1 !important;
    margin-bottom: 0.5rem !important;
}
h2 {
    font-size: 32px !important;
    font-weight: 600 !important;
    letter-spacing: -0.018em !important;
    color: #1d1d1d !important;
}
h3 {
    font-size: 28px !important;
    font-weight: 600 !important;
    letter-spacing: -0.012em !important;
    color: #1d1d1d !important;
}
h4 { font-size: 22px !important; font-weight: 600 !important; color: #1d1d1d !important; }
p, span, label, div { color: #1d1d1d; }

/* ===== KPI Card — neumorphic 凸起卡 ===== */
[data-testid="stMetric"] {
    background: var(--neu-bg);
    border: none;
    border-radius: 24px;
    padding: 1.25rem 1.5rem;
    box-shadow: var(--neu-out);
    transition: transform 0.3s ease-out, box-shadow 0.3s ease-out;
}
[data-testid="stMetric"]:hover {
    transform: translateY(-2px);
    box-shadow: var(--neu-out-hover);
}
[data-testid="stMetricLabel"] {
    color: #6e6e73 !important;
    font-size: 13px !important;
    font-weight: 400 !important;
    text-transform: none !important;
    letter-spacing: 0 !important;
}
[data-testid="stMetricValue"] {
    color: #1d1d1d !important;
    font-weight: 600 !important;
    font-size: 30px !important;
    letter-spacing: -0.018em !important;
}
[data-testid="stMetricDelta"] {
    font-size: 13px !important;
    font-weight: 500 !important;
}

/* ===== Sidebar ===== */
[data-testid="stSidebar"] {
    background: var(--neu-bg);
    border-right: none;
}
[data-testid="stSidebar"] h1,
[data-testid="stSidebar"] h2,
[data-testid="stSidebar"] h3,
[data-testid="stSidebar"] h4,
[data-testid="stSidebar"] h5 {
    letter-spacing: -0.01em !important;
    font-size: 12px !important;
    text-transform: none !important;
    color: #6e6e73 !important;
    margin-top: 1.25rem !important;
    font-weight: 600 !important;
}

/* ===== Dataframe — neumorphic 凸起外壳（内部 canvas 为框架限制保持原样）===== */
[data-testid="stDataFrame"] {
    border: none;
    border-radius: 16px;
    overflow: hidden;
    background: var(--neu-bg);
    box-shadow: var(--neu-out-sm);
    font-size: 16px;
}

/* ===== 图表文字放大（Altair/Vega SVG · Plotly · canvas 表は CSS 不可のため対象外）===== */
[data-testid="stVegaLiteChart"] text,
[data-testid="stVegaLiteChart"] svg text {
    font-size: 15px !important;
}
[data-testid="stPlotlyChart"] text,
.js-plotly-plot text {
    font-size: 15px !important;
}

/* ===== Tabs — 极简下划线 ===== */
[data-baseweb="tab-list"] {
    border-bottom: 1px solid #d2d2d7;
    gap: 1.5rem;
    background: transparent;
}
[data-baseweb="tab"] {
    font-weight: 500 !important;
    font-size: 15px !important;
    color: #6e6e73 !important;
    padding: 0.75rem 0.25rem !important;
    background: transparent !important;
}
[data-baseweb="tab"][aria-selected="true"] {
    color: #1d1d1d !important;
}
[data-baseweb="tab-highlight"] {
    background: #1d1d1d !important;
    height: 2px !important;
}

/* ===== Buttons — neumorphic 凸起 pill（primary = SMIKIE 红强调）===== */
[data-testid="stButton"] > button,
[data-testid="stDownloadButton"] > button,
[data-testid="stFormSubmitButton"] > button {
    border-radius: 980px !important;
    font-weight: 500 !important;
    font-size: 14px !important;
    padding: 0.5rem 1.25rem !important;
    border: none !important;
    background: var(--neu-bg) !important;
    color: #1d1d1d !important;
    box-shadow: var(--neu-out-sm) !important;
    transition: transform 0.3s ease-out, box-shadow 0.3s ease-out !important;
}
[data-testid="stButton"] > button:hover,
[data-testid="stDownloadButton"] > button:hover,
[data-testid="stFormSubmitButton"] > button:hover {
    transform: translateY(-1px);
    box-shadow: var(--neu-out) !important;
}
[data-testid="stButton"] > button:active,
[data-testid="stDownloadButton"] > button:active,
[data-testid="stFormSubmitButton"] > button:active {
    transform: translateY(0.5px);
    box-shadow: var(--neu-in) !important;
}
[data-testid="stButton"] > button[kind="primary"],
[data-testid="stFormSubmitButton"] > button[kind="primary"] {
    background: #d6000f !important;
    color: #ffffff !important;
    font-weight: 600 !important;
    box-shadow: var(--neu-out-sm) !important;
}
[data-testid="stButton"] > button[kind="primary"]:hover,
[data-testid="stFormSubmitButton"] > button[kind="primary"]:hover {
    background: #b8000d !important;
    transform: translateY(-1px);
    box-shadow: var(--neu-out) !important;
}

/* ===== page_link · 统一尺寸/间距,active 用统一深色 pill ===== */
[data-testid="stSidebar"] [data-testid="stPageLink"] {
    margin: 0 !important;
}
[data-testid="stSidebar"] [data-testid="stPageLink"] a {
    border-radius: 10px !important;
    transition: background 0.15s, color 0.15s;
    padding: 8px 12px !important;
    margin: 2px 0 !important;
    height: 36px !important;
    min-height: 36px !important;
    display: flex !important;
    align-items: center !important;
    box-sizing: border-box !important;
    font-size: 18px !important;
    line-height: 1.2 !important;
    background: transparent !important;
    color: #1d1d1d !important;
}
/* 文字 label 在 a 内子元素(span / markdown p)·font-size 设在 a 不会传递 → 直接命中子元素 */
[data-testid="stSidebar"] [data-testid="stPageLink"] a p,
[data-testid="stSidebar"] [data-testid="stPageLink"] a span,
[data-testid="stSidebar"] [data-testid="stPageLink"] a div {
    font-size: 18px !important;
}
[data-testid="stSidebar"] [data-testid="stPageLink"] a:hover {
    background: rgba(0, 0, 0, 0.04) !important;
}
/* active page_link: Streamlit 把当前页的 href 设为空字符串作为标记
   全站选中态统一: 浅蓝背景 #fbe5e3 + 深蓝字 #a8000c + 加粗 */
[data-testid="stSidebar"] [data-testid="stPageLink-NavLink"][href=""],
[data-testid="stSidebar"] [data-testid="stPageLink-NavLink"][href$="/"]:not([href*="?"]) {
    background: #fbe5e3 !important;
    color: #a8000c !important;
    font-weight: 600 !important;
}
[data-testid="stSidebar"] [data-testid="stPageLink-NavLink"][href=""] *,
[data-testid="stSidebar"] [data-testid="stPageLink-NavLink"][href$="/"]:not([href*="?"]) * {
    color: #a8000c !important;
}

/* ===== 输入框 / 选择框：neumorphic 凹陷井 ===== */
[data-baseweb="input"] input,
[data-baseweb="select"] > div,
[data-baseweb="textarea"] textarea {
    border-radius: 16px !important;
    border: none !important;
    background: var(--neu-bg) !important;
    box-shadow: var(--neu-in) !important;
    font-size: 14px !important;
}
[data-baseweb="input"]:focus-within input,
[data-baseweb="select"]:focus-within > div {
    box-shadow: var(--neu-in-deep), 0 0 0 2px rgba(214, 0, 15, 0.4) !important;
}
/* 下拉菜单文字放大：selectbox/multiselect 选中值 + 展开选项（默认 14px 偏小）*/
[data-baseweb="select"] > div,
[data-baseweb="select"] span,
[data-baseweb="popover"] [role="option"],
[data-baseweb="popover"] li,
[data-baseweb="menu"] li {
    font-size: 17px !important;
}

/* ===== Radio / Checkbox — 苹果蓝 ===== */
[data-baseweb="radio"] [data-checked="true"] {
    background-color: #d6000f !important;
    border-color: #d6000f !important;
}
[data-testid="stCheckbox"] [data-checked="true"] {
    background-color: #d6000f !important;
    border-color: #d6000f !important;
}

/* ===== Multiselect 选中标签 — 浅色（默认深蓝太重）===== */
[data-baseweb="tag"] {
    background-color: #eef0f3 !important;
    color: #1d1d1d !important;
    border: 1px solid #d2d2d7 !important;
}
[data-baseweb="tag"] span { color: #1d1d1d !important; }
[data-baseweb="tag"] svg { fill: #6e6e73 !important; }

/* ===== Expander — neumorphic 凸起卡 ===== */
[data-testid="stExpander"] {
    border: none !important;
    border-radius: 16px !important;
    background: var(--neu-bg) !important;
    box-shadow: var(--neu-out-sm) !important;
}
[data-testid="stExpander"] summary {
    font-weight: 500 !important;
    color: #1d1d1d !important;
}

/* ===== Alert / Info / Warning — neumorphic 凹陷信息条 ===== */
[data-testid="stAlert"] {
    border-radius: 16px !important;
    border: none !important;
    background: var(--neu-bg) !important;
    box-shadow: var(--neu-in) !important;
}

/* ===== Divider — 极淡 ===== */
hr {
    border-color: #d2d2d7 !important;
    margin: 2rem 0 !important;
}

/* ===== caption ===== */
[data-testid="stCaptionContainer"], small {
    color: #6e6e73 !important;
    font-size: 13px !important;
    letter-spacing: -0.005em !important;
}

/* ===== 风险胸章 — 苹果系统色卡 ===== */
.badge-A,
.badge-B,
.badge-C,
.badge-NEW,
.badge-RED {
    padding: 2px 10px;
    border-radius: 980px;
    font-size: 11px;
    font-weight: 500;
    display: inline-block;
    letter-spacing: -0.005em;
}
.badge-A { background: rgba(52, 199, 89, 0.12); color: #1f8a3c; }
.badge-B { background: rgba(214, 0, 15, 0.10); color: #a8000c; }
.badge-C { background: rgba(142, 142, 147, 0.14); color: #515154; }
.badge-NEW { background: rgba(255, 149, 0, 0.14); color: #b56400; }
.badge-RED { background: rgba(255, 59, 48, 0.12); color: #b32419; }

/* ===== Density toggle (page 18) ===== */
.density-compact [data-testid="stDataFrame"] td,
.density-compact [data-testid="stDataFrame"] th {
    padding: 0.3rem 0.6rem !important;
    font-size: 12.5px !important;
}
.density-comfy [data-testid="stDataFrame"] td,
.density-comfy [data-testid="stDataFrame"] th {
    padding: 0.95rem 1rem !important;
    font-size: 14px !important;
}

/* ===== 主容器 padding 收紧 ===== */
[data-testid="stMainBlockContainer"], .main .block-container {
    padding-top: 2rem !important;
    padding-bottom: 4rem !important;
    max-width: 1400px;
}

/* ===== 滚动条：极简灰 ===== */
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-thumb { background: rgba(0, 0, 0, 0.18); border-radius: 980px; border: 2px solid transparent; background-clip: content-box; }
::-webkit-scrollbar-thumb:hover { background: rgba(0, 0, 0, 0.28); border: 2px solid transparent; background-clip: content-box; }
::-webkit-scrollbar-track { background: transparent; }

/* ===== 整体缩放 90%（Boss 2026-05-20 · 全页面统一 -10%）===== */
.stApp { zoom: 0.9; }
</style>
"""


def inject_theme() -> None:
    """注入全局 Apple 风格主题 CSS（每页 require_password 之后调用一次）。"""
    st.markdown(_THEME_CSS, unsafe_allow_html=True)


__all__ = ["inject_theme"]
