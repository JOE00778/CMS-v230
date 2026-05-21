"""全局 UI 主题 · Apple Design Language

苹果设计语言核心：
- SF Pro / -apple-system 字体栈，负字距大标题
- #f5f5f7 系统灰背景 + 纯白卡片 + 1px hairline 边框（#d2d2d7）
- 单一系统蓝 #0071e3 作为强调色，避免多色混用
- 大留白（24-40px gap），柔和阴影（rgba(0,0,0,0.04)）
- 12-18px 圆角；按钮 pill / 980px-radius
- 无 ALL CAPS、无重粗体大写小标题
- 减号字距 letter-spacing: -0.022em 用于标题

用法：每个 page 顶部调用 inject_theme()，建议放在 require_password() 之后。
提供 .badge-A/.badge-B/.badge-C/.badge-NEW/.badge-RED 类供 page 使用。
"""
from __future__ import annotations

import streamlit as st

_THEME_CSS = """
<style>
/* ===== Apple System Fonts =====
   优先 SF Pro（macOS/iOS 自带），降级 Inter / Noto Sans JP */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Noto+Sans+JP:wght@400;500;700&display=swap');

/* 字体：只设 html/body，让继承传递；不覆盖 Streamlit 内部
   为图标 span 设置的 Material Symbols Outlined 字体（否则 ligature 失效，
   会露出 keyboard_arrow_down / keyboard_arrow_right 等原始文本） */
html, body {
    font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'SF Pro Text',
                 'Inter', 'Noto Sans JP', 'Helvetica Neue', Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
}

/* ===== App 背景 ===== */
[data-testid="stAppViewContainer"] {
    background: #f5f5f7;
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
    color: #1d1d1f !important;
    line-height: 1.1 !important;
    margin-bottom: 0.5rem !important;
}
h2 {
    font-size: 28px !important;
    font-weight: 600 !important;
    letter-spacing: -0.018em !important;
    color: #1d1d1f !important;
}
h3 {
    font-size: 22px !important;
    font-weight: 600 !important;
    letter-spacing: -0.012em !important;
    color: #1d1d1f !important;
}
h4 { font-size: 18px !important; font-weight: 600 !important; color: #1d1d1f !important; }
p, span, label, div { color: #1d1d1f; }

/* ===== KPI Card — 苹果风纯白卡 ===== */
[data-testid="stMetric"] {
    background: #ffffff;
    border: 1px solid #d2d2d7;
    border-radius: 18px;
    padding: 1.25rem 1.5rem;
    box-shadow: none;
    transition: transform 0.2s ease, box-shadow 0.2s ease;
}
[data-testid="stMetric"]:hover {
    transform: translateY(-1px);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.06);
}
[data-testid="stMetricLabel"] {
    color: #6e6e73 !important;
    font-size: 13px !important;
    font-weight: 400 !important;
    text-transform: none !important;
    letter-spacing: 0 !important;
}
[data-testid="stMetricValue"] {
    color: #1d1d1f !important;
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
    background: rgba(255, 255, 255, 0.72);
    backdrop-filter: saturate(180%) blur(20px);
    -webkit-backdrop-filter: saturate(180%) blur(20px);
    border-right: 1px solid rgba(0, 0, 0, 0.06);
}
[data-testid="stSidebar"] h1,
[data-testid="stSidebar"] h2,
[data-testid="stSidebar"] h3,
[data-testid="stSidebar"] h4,
[data-testid="stSidebar"] h5 {
    letter-spacing: -0.01em !important;
    font-size: 13px !important;
    text-transform: none !important;
    color: #6e6e73 !important;
    margin-top: 1.25rem !important;
    font-weight: 600 !important;
}

/* ===== Dataframe ===== */
[data-testid="stDataFrame"] {
    border: 1px solid #d2d2d7;
    border-radius: 14px;
    overflow: hidden;
    background: #ffffff;
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
    color: #1d1d1f !important;
}
[data-baseweb="tab-highlight"] {
    background: #1d1d1f !important;
    height: 2px !important;
}

/* ===== Buttons — Apple pill ===== */
[data-testid="stButton"] > button,
[data-testid="stDownloadButton"] > button,
[data-testid="stFormSubmitButton"] > button {
    border-radius: 980px !important;
    font-weight: 500 !important;
    font-size: 14px !important;
    padding: 0.5rem 1.25rem !important;
    border: 1px solid #d2d2d7 !important;
    background: #ffffff !important;
    color: #1d1d1f !important;
    box-shadow: none !important;
    transition: background 0.15s ease;
}
[data-testid="stButton"] > button:hover,
[data-testid="stDownloadButton"] > button:hover,
[data-testid="stFormSubmitButton"] > button:hover {
    background: #f5f5f7 !important;
    border-color: #1d1d1f !important;
}
[data-testid="stButton"] > button[kind="primary"],
[data-testid="stFormSubmitButton"] > button[kind="primary"] {
    background: #dbeafe !important;
    color: #0058b0 !important;
    border-color: #0071e3 !important;
    font-weight: 600 !important;
}
[data-testid="stButton"] > button[kind="primary"]:hover,
[data-testid="stFormSubmitButton"] > button[kind="primary"]:hover {
    background: #c5dbfb !important;
    border-color: #0058b0 !important;
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
    font-size: 14px !important;
    line-height: 1.2 !important;
    background: transparent !important;
    color: #1d1d1f !important;
}
[data-testid="stSidebar"] [data-testid="stPageLink"] a:hover {
    background: rgba(0, 0, 0, 0.04) !important;
}
/* active page_link: Streamlit 把当前页的 href 设为空字符串作为标记
   全站选中态统一: 浅蓝背景 #dbeafe + 深蓝字 #0058b0 + 加粗 */
[data-testid="stSidebar"] [data-testid="stPageLink-NavLink"][href=""],
[data-testid="stSidebar"] [data-testid="stPageLink-NavLink"][href$="/"]:not([href*="?"]) {
    background: #dbeafe !important;
    color: #0058b0 !important;
    font-weight: 600 !important;
}
[data-testid="stSidebar"] [data-testid="stPageLink-NavLink"][href=""] *,
[data-testid="stSidebar"] [data-testid="stPageLink-NavLink"][href$="/"]:not([href*="?"]) * {
    color: #0058b0 !important;
}

/* ===== 输入框 / 选择框：圆角 + hairline ===== */
[data-baseweb="input"] input,
[data-baseweb="select"] > div,
[data-baseweb="textarea"] textarea {
    border-radius: 12px !important;
    border-color: #d2d2d7 !important;
    background: #ffffff !important;
    font-size: 14px !important;
}
[data-baseweb="input"]:focus-within input,
[data-baseweb="select"]:focus-within > div {
    border-color: #0071e3 !important;
    box-shadow: 0 0 0 3px rgba(0, 113, 227, 0.15) !important;
}

/* ===== Radio / Checkbox — 苹果蓝 ===== */
[data-baseweb="radio"] [data-checked="true"] {
    background-color: #0071e3 !important;
    border-color: #0071e3 !important;
}
[data-testid="stCheckbox"] [data-checked="true"] {
    background-color: #0071e3 !important;
    border-color: #0071e3 !important;
}

/* ===== Expander — 圆角卡 ===== */
[data-testid="stExpander"] {
    border: 1px solid #d2d2d7 !important;
    border-radius: 14px !important;
    background: #ffffff !important;
    box-shadow: none !important;
}
[data-testid="stExpander"] summary {
    font-weight: 500 !important;
    color: #1d1d1f !important;
}

/* ===== Alert / Info / Warning ===== */
[data-testid="stAlert"] {
    border-radius: 14px !important;
    border: 1px solid #d2d2d7 !important;
    background: #ffffff !important;
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
.badge-B { background: rgba(0, 113, 227, 0.10); color: #0058b0; }
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
