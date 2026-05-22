# CMS 主题格式规范（Theme Format Spec）

> 给「要把外部主题翻译进 CMS」的人/工具看：CMS 的主题**不是**自由 HTML+CSS（没有 `.kpi-card` `.sidebar` 这种自定义 class），
> 而是 **Streamlit（1.57）的两层主题系统**。外部主题必须按下面的「层 + 选择器词汇表 + 槽位」重新表达。
> 对应文件：`shared/theme.py`（366 行 CSS 注入）+ `.streamlit/config.toml`（原生主题层）。基准 2026-05-22。

---

## 1. 总体格式：两层，缺一不可

| 层 | 文件 | 管什么 | 改的方式 |
|---|---|---|---|
| **L1 原生主题** | `.streamlit/config.toml` `[theme]` | 控件焦点色、**st.dataframe 表头底色（canvas，CSS 进不去）**、默认字体族、圆角基线 | TOML 键值 |
| **L2 CSS 覆盖** | `shared/theme.py` | 其余一切外观：背景、标题、卡片、侧栏、按钮、tab、输入、badge、滚动条、缩放 | 一段 `<style>` 字符串 |

> **规则**：L1 定底色基调，L2 用 CSS 精修。两层必须同步，否则撕裂（如 L2 改了主色、但 dataframe 表头仍是 L1 旧色）。

---

## 2. L1 格式 —— `.streamlit/config.toml` `[theme]`

值类型：颜色 = `"#RRGGBB"` 字符串；字体 = 字体族名或列表。CMS 当前用到 5 个键，Streamlit 1.57 还支持更多槽位：

```toml
[theme]
# —— CMS 当前在用 ——
primaryColor = "#0071e3"             # 主色：控件强调/焦点/primary 控件
backgroundColor = "#f5f5f7"          # App 主背景
secondaryBackgroundColor = "#ffffff" # 次背景：sidebar、dataframe 表头、code 块
textColor = "#1d1d1f"                # 全局文字
font = "sans serif"                  # "sans serif" | "serif" | "monospace" | 自定义族名

# —— 1.57 还可用（CMS 未用，外部主题若需要可加）——
# base = "light"                     # 明暗基底
# linkColor = "#0071e3"              # 超链接
# codeBackgroundColor = "#f5f5f7"    # 代码块底
# baseRadius = "0.75rem"             # 全局圆角基线（small/medium/large/full 或 rem）
# borderColor = "#E2E8F0"            # 控件描边
# baseFontSize = 16                  # 基础字号（px）

# —— 自定义字体（原生方式，替代 CSS @import，可指本地文件不连外网）——
# [[theme.fontFaces]]
# family = "Inter"
# url = "app/static/Inter.woff2"     # 放 CMS/static/，本地加载
# weight = 400
```
其它非主题段（勿动）：`[server] maxUploadSize/enableXsrfProtection`、`[client] showSidebarNavigation=false`（← CMS 用自定义导航，见 §6）。

---

## 3. L2 格式 —— `shared/theme.py`

### 结构（固定形态）
```python
_THEME_CSS = """
<style>
  /* 纯 CSS，全靠属性选择器命中 Streamlit 生成的 DOM */
</style>
"""
def inject_theme() -> None:
    st.markdown(_THEME_CSS, unsafe_allow_html=True)
```
- **整个主题 = 一段 `<style>` 字符串**。外部主题的所有 CSS 最终塞进这里。
- 没有自定义 class 体系；要改某个元素，**必须知道它对应的 Streamlit 选择器**（见 §4）。

### 消费约定
每个 page 顶部、`require_password()` **之后**调一次 `inject_theme()`。`cms.py` 第 34–35 行示范：
```python
require_password()
inject_theme()
```

---

## 4. 选择器词汇表（格式的灵魂）

Streamlit 不给语义 class，只能用 `[data-testid="…"]`（Streamlit 容器）和 `[data-baseweb="…"]`（BaseWeb 控件）命中。
**外部主题的每个视觉概念，必须映射到下表的选择器**，不能用原模板的 class 名。

| 外部模板概念 | CMS 必须用的选择器 | 槽位说明 |
|---|---|---|
| `body` / 全局字体 | `html, body` | 字体栈 + 基础字号（rem 源头） |
| App 背景 | `[data-testid="stAppViewContainer"]` | 主背景色 |
| 顶栏 | `[data-testid="stHeader"]` | 现设透明 |
| 主内容容器 | `[data-testid="stMainBlockContainer"]`, `.main .block-container` | padding / max-width |
| `h1..h4` 标题 | `h1` `h2` `h3` `h4`（全局，需 `!important`） | 字号/字重/颜色/字距 |
| 正文 | `p, span, label, div` | 文字色 |
| `.kpi-card` | `[data-testid="stMetric"]` + `…stMetricLabel/stMetricValue/stMetricDelta` | KPI 卡（`st.metric` 渲染） |
| `.sidebar` | `[data-testid="stSidebar"]` | 侧栏底色/描边/毛玻璃 |
| `.nav-item` | `[data-testid="stPageLink"] a`；**active** = `[data-testid="stPageLink-NavLink"][href=""]` | 导航项；当前页 href 被置空作标记 |
| 表格 | `[data-testid="stDataFrame"]`（外框）；**单元格=canvas，CSS 不入** | 只能改外框；表头底色靠 L1 |
| `.btn` / `.btn-primary` | `[data-testid="stButton"|"stDownloadButton"|"stFormSubmitButton"] > button`；primary = `button[kind="primary"]` | 按钮 |
| 输入框 | `[data-baseweb="input"] input`, `[data-baseweb="textarea"] textarea` | |
| 下拉/选择 | `[data-baseweb="select"]`, `[data-baseweb="popover"] [role="option"]`, `[data-baseweb="menu"] li` | 选中值 + 展开项 |
| 多选标签 | `[data-baseweb="tag"]` | |
| Tabs | `[data-baseweb="tab-list"|"tab"|"tab-highlight"]`；选中 = `[data-baseweb="tab"][aria-selected="true"]` | 下划线式 |
| Radio / Checkbox | `[data-baseweb="radio"] [data-checked="true"]`, `[data-testid="stCheckbox"] [data-checked="true"]` | |
| Expander | `[data-testid="stExpander"]` + `summary` | |
| Alert/Info/Warn | `[data-testid="stAlert"]` | 统一卡样式 |
| 进度条 | `[data-testid="stProgress"] > div > div > div` | 模板那种渐变进度条套这里 |
| Caption/小字 | `[data-testid="stCaptionContainer"]`, `small` | |
| 折线/面积图文字 | `[data-testid="stVegaLiteChart"] text`, `[data-testid="stPlotlyChart"] text`, `.js-plotly-plot text` | 只能改文字；图形配色在各 page 的 plotly/altair 代码里 |
| 分隔线 | `hr` | |
| 滚动条 | `::-webkit-scrollbar` / `-thumb` / `-track` | |
| 全站缩放 | `.stApp { zoom: … }` | 现为 0.9 |

---

## 5. CMS 自定义类契约（外部主题须保留类名）

这些 class 是 CMS 在 `theme.py` 定义、page 通过 `st.markdown(..., unsafe_allow_html=True)` 使用的。**外部主题可改样式，但不能改类名**（page 依赖）：

| 类名 | 用途 | 现样式骨架 |
|---|---|---|
| `.badge-A` `.badge-B` `.badge-C` `.badge-NEW` `.badge-RED` | 风险/状态胸章 | `padding:2px 10px; border-radius:980px; font-size:11px; 浅底+同色字` |
| `.density-compact` `.density-comfy` | page18 表格密度切换 | 调 `stDataFrame td/th` 的 padding/font-size |

---

## 6. 格式硬规则（踩过的坑，必须遵守）

1. **覆盖几乎都要 `!important`** —— Streamlit 行内样式优先级高，不加不生效。
2. **`font-size` 不向子元素继承** —— 典型：`stPageLink a` 设字号无效，文字在 `a` 的子 `p/span/div` 里，必须**显式命中子元素**（theme.py 第 215–219 行就是为此）。
3. **基础字号在 `html,body`（现 21px）** 驱动 rem；全站 `.stApp{zoom:0.9}` 再统一缩 10%。
4. **不要 `@import` 外部字体**（数据安全策略）。要自定义字体走 L1 的 `[[theme.fontFaces]]` 指**本地** `static/` 文件；CJK 一律用系统字体（苹方/ヒラギノ/雅黑/游ゴ），不下载（Noto CJK 太大）。
5. **CJK 字形靠 `system-ui` 按 OS locale 自适应** —— Mac 出苹方/ヒラギノ，元川 Windows 出雅黑/游ゴ。
6. **导航 `showSidebarNavigation=false`**，导航是 `cms.py` 用 `st.page_link` 自绘 —— 主题只能改其样式，结构动不了。

---

## 7. 不可主题化清单（外部主题这些做不到，需降级）

| 外部主题元素 | 原因 | CMS 替代 |
|---|---|---|
| 表格单元格 badge / 行 hover / 自定义表头 | `st.dataframe` 是 **canvas 渲染**，CSS 进不去 | 只能改外框；要效果得换 HTML 表/AgGrid（重） |
| 侧栏 SVG 图标导航 | `st.page_link` 不支持自定义图标 | 用 emoji 前缀（page 文件名已带） |
| Chart.js 渐变面积线 / donut / radar / sparkline | CMS 用 plotly/altair，非 Chart.js | 逐页改图表代码（`pages/04 05 06 16 18 20 28`），**非主题层** |
| 渐变 logo 文字 | 侧栏顶部由 Streamlit 生成 | 可在 `cms.py` 用 `st.markdown` 注入自定义 logo |

---

## 8. 一句话交接

> 把外部主题的**配色/字体/圆角/阴影/间距**这些 token，按 §4 的「概念→选择器」映射表，
> 写进 `shared/theme.py` 的 `_THEME_CSS`（CSS）+ `.streamlit/config.toml`（§2 的键）。
> 结构性的东西（侧栏图标、表格单元格、图表内部）按 §7 降级，不要硬塞。
