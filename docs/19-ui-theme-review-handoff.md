# CMS UI 主题 / 布局 审查交接包

> 给外部 AI 或人审查：这是 SmikieJapan「一元管理系统 V2.4」（Streamlit 经营看板）经过一整轮 UI 改造后的**完整样式状态 + 踩坑记录 + 未解决问题**。
> 目标：找出哪里还能修正（字体、宽度、对齐、图表/表格填充等）。
> 生成日期：2026-05-23 · 线上 = smikie-cms.cc（CF Access 保护）· commit `c0f0fac`

---

## 1. 技术栈 & 架构（审查前必读）

- **Streamlit 1.57.0**（已是 PyPI 最新版，不能再升）多页 App，27 个 page（`pages/NN_*.py`）。
- **样式两层**：
  1. **L1 `.streamlit/config.toml`** `[theme]` —— Streamlit 原生主题键（primaryColor 等）。管 CSS 够不到的地方（控件焦点色、**st.dataframe 表头底色**——它是 canvas 渲染）。
  2. **L2 `shared/theme.py`** —— 一段全局 `<style>`，每页 `inject_theme()` 注入。**几乎所有外观都在这。**
- **页面生命周期**：每页顶部 `st.set_page_config(layout="wide")` → `require_password()`（注入 auth.py 的紧凑 layout CSS）→ `inject_theme()` → `lang_selector()`（i18n.py，注入语言切换器 CSS + 渲染侧栏导航）。
- **全局缩放**：`.stApp { zoom: 0.90 }`（⚠️ 见「已知问题」——疑似干扰 Vega 图表宽度测量）。
- **字体**：本地打包 woff2（`static/fonts/`，经 `enableStaticServing` 暴露），不连 CDN（数据安全要求）。
- **部署**：元川 Windows / Docker bind-mount，`git pull` + `docker restart cms_streamlit` 立即生效（代码改动不需 build）。

### Streamlit 关键限制（已实测确认，审查者请勿绕弯）
1. **`st.dataframe` 是 glide-data-grid canvas 渲染**：单元格字号 CSS 改不了；`use_container_width=True` / `width="stretch"` **不会把列拉伸填满**容器——列总和小于容器时右侧留内部空白，且无任何 Streamlit API 能强制列拉伸。（老版 1.0 的 app 跑更老 streamlit，那时会拉伸，1.57 不会。）
2. **图表是 Altair/Vega（svg/canvas）**：绘图区宽度/边距由 Vega 内部算，外部 CSS 难以安全干预（一动就裁切，见踩坑）。

---

## 2. 完整源码

### 2.1 `.streamlit/config.toml`
```toml
[theme]
primaryColor = "#4F46E5"
backgroundColor = "#F1F5F9"
secondaryBackgroundColor = "#FFFFFF"
textColor = "#0F172A"
font = "sans serif"

[server]
maxUploadSize = 200
enableCORS = false
enableXsrfProtection = true
enableStaticServing = true

[browser]
gatherUsageStats = false

[client]
showSidebarNavigation = false
```

### 2.2 `shared/theme.py` 的全局 CSS（核心审查对象）
```css
<style>
/* ===== 字体加载（本地打包·不连外网）Inter→拉丁 · Noto Sans SC→中文 · Noto Sans JP→日文 ===== */
@font-face { font-family:'Inter'; src:url('app/static/fonts/Inter-400.woff2') format('woff2'); font-weight:400; font-display:swap; }
/* …Inter 500/600/700 · NotoSansSC 400/700 · NotoSansJP 400/700 同理… */

/* ===== 字体栈（按字符自动选字；只设 html/body+显式文字元素，不用 [class*=st-] 大锤，否则盖掉 Material Symbols 图标）===== */
html, body,
[data-testid="stPageLink"] a, [data-testid="stPageLink"] a p, [data-testid="stPageLink"] a span,
[data-baseweb="tab"], [data-baseweb="select"] div, [data-baseweb="select"] span,
[data-baseweb="popover"] li, [data-baseweb="menu"] li, [data-baseweb="input"] input,
button, textarea, select {
  font-family: 'Inter','Noto Sans SC','Noto Sans JP','PingFang SC','Hiragino Sans','Microsoft YaHei','Yu Gothic',system-ui,-apple-system,sans-serif !important;
}
[class*="material-symbols"], [data-testid="stIconMaterial"], .material-icons {
  font-family: 'Material Symbols Rounded','Material Symbols Outlined','Material Symbols Sharp','Material Icons' !important;
}
html, body { font-size: 21px; -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale; }

/* ===== App 背景 ===== */
[data-testid="stAppViewContainer"] { background: #F1F5F9 !important; }
[data-testid="stHeader"] { background: transparent !important; border-bottom: none !important; height: auto !important; }

/* ===== 标题与正文 ===== */
h1, h2, h3, h4 { color: #0F172A !important; font-weight: 700 !important; letter-spacing: -0.025em !important; margin-bottom: 0.5rem !important; }
h1 { font-size: 36px !important; line-height: 1.15 !important; }
h2 { font-size: 30px !important; }
h3 { font-size: 24px !important; }
h4 { font-size: 20px !important; font-weight: 600 !important; }
p, span, label, div { color: #475569 !important; }

/* ===== KPI 卡片 (stMetric) ===== */
[data-testid="stMetric"] { background:#FFFFFF !important; border:1px solid #E2E8F0 !important; border-radius:16px !important; padding:18px 16px !important; box-shadow:0 1px 3px rgba(0,0,0,0.05) !important; transition:transform .2s, box-shadow .2s; }
[data-testid="stMetric"]:hover { transform:translateY(-2px) !important; box-shadow:0 8px 24px rgba(0,0,0,0.08) !important; border-color:#4F46E5 !important; }
[data-testid="stMetricLabel"], [data-testid="stMetricLabel"] p, [data-testid="stMetricLabel"] div { color:#64748B !important; font-size:12px !important; font-weight:500 !important; text-transform:none !important; }
[data-testid="stMetricValue"] { color:#0F172A !important; font-size:24px !important; font-weight:700 !important; letter-spacing:-0.01em !important; white-space:normal !important; overflow:visible !important; line-height:1.25 !important; font-variant-numeric:tabular-nums !important; font-feature-settings:"tnum" 1 !important; }
[data-testid="stMetricValue"] > div { white-space:normal !important; overflow:visible !important; text-overflow:clip !important; }
[data-testid="stMetricDelta"] { font-size:13px !important; font-weight:600 !important; font-variant-numeric:tabular-nums !important; }

/* ===== Sidebar（固定宽 280px）===== */
[data-testid="stSidebar"] { background:#FFFFFF !important; border-right:1px solid #E2E8F0 !important; box-shadow:2px 0 10px rgba(0,0,0,0.02) !important; min-width:280px !important; max-width:280px !important; width:280px !important; }
[data-testid="stSidebar"] h1,h2,h3,h4 { color:#64748B !important; font-size:11px !important; font-weight:600 !important; text-transform:uppercase !important; letter-spacing:1.5px !important; margin-top:1rem !important; }

/* ===== Dataframe（⚠️ fit-content+margin auto 居中 = 最新一次「居中」尝试，glide 敏感，待审查）===== */
[data-testid="stDataFrame"] { background:#FFFFFF !important; border:1px solid #E2E8F0 !important; border-radius:16px !important; width:fit-content !important; max-width:100% !important; margin-left:auto !important; margin-right:auto !important; }

/* ===== 图表容器（text-align:center 居中绘图区 = 当前方案；padding 横向 6px）===== */
[data-testid="stVegaLiteChart"], [data-testid="stPlotlyChart"], .chart-container {
  background:#FFFFFF !important; padding:16px 6px !important; border-radius:16px !important; border:1px solid #E2E8F0 !important; box-shadow:0 1px 3px rgba(0,0,0,0.05) !important; text-align:center !important;
}
[data-testid="stVegaLiteChart"] text, [data-testid="stPlotlyChart"] text { font-family:inherit !important; font-size:14px !important; }

/* ===== Tabs（粗体 700 / 14px）===== */
[data-baseweb="tab-list"] { border-bottom:1px solid #E2E8F0 !important; gap:1rem !important; }
[data-baseweb="tab"] { color:#94A3B8 !important; font-weight:700 !important; font-size:14px !important; padding:.5rem .75rem !important; background:transparent !important; }
[data-baseweb="tab"] p { font-size:14px !important; font-weight:700 !important; }
[data-baseweb="tab"][aria-selected="true"] { color:#4F46E5 !important; border-bottom:2px solid #4F46E5 !important; }
[data-baseweb="tab-highlight"] { background:transparent !important; height:0 !important; }

/* ===== Buttons（10px 圆角；primary 实心靛蓝；secondary 描边）===== */
[data-testid="stButton"]>button, [data-testid="stDownloadButton"]>button, [data-testid="stFormSubmitButton"]>button { border-radius:10px !important; font-weight:500 !important; font-size:14px !important; padding:.55rem 1.25rem !important; transition:all .2s; }
... >button:not([kind="primary"]) { background:transparent !important; border:1px solid #E2E8F0 !important; color:#475569 !important; }
... >button:not([kind="primary"]):hover { border-color:#4F46E5 !important; color:#4F46E5 !important; background:rgba(79,70,229,.05) !important; }
... >button[kind="primary"] { background:#4F46E5 !important; border:1px solid #4F46E5 !important; color:#FFF !important; }
... >button[kind="primary"]:hover { background:#4338CA !important; box-shadow:0 4px 12px rgba(79,70,229,.3) !important; }

/* ===== Navigation 侧栏导航 (stPageLink · 字号 17px · active 靛蓝)===== */
[data-testid="stSidebar"] [data-testid="stPageLink"] a { border-radius:10px !important; padding:10px 12px !important; margin:4px 0 !important; height:40px !important; display:flex !important; align-items:center !important; color:#475569 !important; background:transparent !important; }
... a:hover { background:rgba(79,70,229,.06) !important; color:#4F46E5 !important; }
... a p, ... a span { font-size:17px !important; font-weight:500 !important; }
/* active: Streamlit 把当前页 href 设空字符串作标记 */
... [data-testid="stPageLink-NavLink"][href=""] { background:rgba(79,70,229,.1) !important; color:#4F46E5 !important; border:1px solid rgba(79,70,229,.2) !important; font-weight:600 !important; }

/* ===== Inputs / Selects（圆角 10px · focus 靛蓝 · 文字垂直居中 · 文字 14px 含 placeholder）===== */
[data-baseweb="input"] input, [data-baseweb="select"]>div, [data-baseweb="textarea"] textarea { border-radius:10px !important; border-color:#E2E8F0 !important; background:#F8FAFC !important; }
...:focus-within { border-color:#4F46E5 !important; box-shadow:0 0 0 2px rgba(79,70,229,.15) !important; }
[data-baseweb="select"]>div { display:flex !important; align-items:center !important; }   /* 垂直居中 */
[data-baseweb="input"] { display:flex !important; align-items:center !important; }
[data-baseweb="select"] div/span, [data-baseweb="popover"] li/[role=option], [data-baseweb="menu"] li, ul[role=listbox] li, input, textarea { font-size:14px !important; }  /* 框内/菜单/输入 统一 14px */

/* ===== caption 全站 12px ===== */
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p { font-size:12px !important; }

/* ===== Progress / Radio / Checkbox / Tag / Alert / Expander（略，均靛蓝系 + 圆角）===== */
[data-testid="stProgress"] > div>div>div { background:linear-gradient(90deg,#4F46E5,#0EA5E9) !important; }
[data-testid="stExpander"] summary p { font-size:13px !important; }   /* expander 标题 13px */

/* ===== CMS 自定义契约类（page 代码依赖，不可改类名）===== */
.badge-A/B/C/NEW/RED { padding:2px 10px; border-radius:980px; font-size:11px; font-weight:600; display:inline-block; }  /* A绿 B靛蓝 C灰 NEW橙 RED红 */
.density-compact / .density-comfy [data-testid="stDataFrame"] td/th { ... }

/* ===== Main Container（固定 1400px 居中 = 当前布局核心）===== */
[data-testid="stMainBlockContainer"], .main .block-container {
  padding:1.5rem; padding-bottom:3rem; max-width:1400px !important; margin-left:auto !important; margin-right:auto !important;
}

/* ===== Scrollbar 极简灰（略）===== */

/* ===== HTML 满宽表格（html_table 助手用·只有店铺毛利页 page05 在用）===== */
.cms-table-wrap { width:100% !important; overflow-x:auto; border:1px solid #E2E8F0; border-radius:16px; background:#FFFFFF; }
.cms-table { width:100% !important; border-collapse:collapse; font-size:14px; table-layout:auto; }
.cms-table thead th { background:#F8FAFC; color:#64748B; font-weight:600; font-size:12px; text-align:left; padding:10px 14px; border-bottom:1px solid #E2E8F0; white-space:nowrap; }
.cms-table tbody td { padding:9px 14px; color:#0F172A; border-bottom:1px solid #F1F5F9; font-variant-numeric:tabular-nums; white-space:nowrap; }
.cms-table td.num, .cms-table th.num { text-align:right; }

/* ===== 全局缩放（⚠️ 疑似干扰 Vega 测量）===== */
.stApp { zoom: 0.90 !important; }
</style>
```
> 完整未删节版见仓库 `shared/theme.py`（419 行）。上面省略了重复的 @font-face 和部分一目了然的块，结构与命名完整保留。

### 2.3 `html_table()` 助手（shared/theme.py 内）
把已格式化为字符串的 DataFrame 渲染成 **满宽 HTML 表**（`<table width:100%>` 列自动拉伸，右边缘与图表/页面对齐；数字列右对齐 + tabular-nums）。**目前只有店铺毛利页（page 05）6 张表在用**，其余 24 页仍是 `st.dataframe`。大表（2000 行）不建议用 HTML（性能）。

---

## 3. 字体体系

**字体栈**（浏览器按字符逐个选第一个有该字形的）：
`'Inter','Noto Sans SC','Noto Sans JP','PingFang SC','Hiragino Sans','Microsoft YaHei','Yu Gothic',system-ui,sans-serif`
- 拉丁/数字 → Inter；中文 → Noto Sans SC；日文 → Noto Sans JP（⚠️ SC 排 JP 前，**日文汉字会用中文字形**，中日异形字对日文读者略不地道——已知取舍，CMS 中文为主）。

**各元素字号一览**：

| 元素 | 字号 |
|---|---|
| 基础正文 html/body | 21px |
| h1 / h2 / h3 / h4 | 36 / 30 / 24 / 20px（700 粗，h4=600）|
| KPI 数字 stMetricValue | 24px（tabular-nums）|
| KPI 标签 / delta | 12 / 13px |
| 侧边导航 stPageLink | 17px |
| 侧栏分组标题 | 11px 大写 |
| Tab | 14px 粗体 700 |
| 按钮 | 14px |
| 下拉框/菜单/输入/placeholder | 14px |
| caption（标题下灰字说明）| 12px |
| expander 标题 | 13px |
| 图表内文字 | 14px |
| HTML 表 html_table | 表头 12px / 单元格 14px |

---

## 4. 布局体系

- **内容区固定 `max-width:1400px` + `margin:auto` 居中**（两侧对称留白）。这是「整体页对齐居中」的最终方案。
- **侧栏固定 280px**。
- **全局 `zoom:0.90`**。
- 配色：背景 `#F1F5F9` / 卡片 `#FFFFFF` / 边框 `#E2E8F0` / 主色靛蓝 `#4F46E5` / 副色 sky `#0EA5E9` / 主文字 `#0F172A` / 次文字 `#475569` / 弱 `#64748B`/`#94A3B8`。圆角 16px（卡）/10px（按钮、输入）。

---

## 5. ⚠️ 已知问题 & 试错记录（**审查重点**）

整轮改造卡在两件事：**表格列填满**和**图表绘图区填满/对齐**。下面是试过的方法和结果，避免审查者重走弯路。

### 5.1 表格（st.dataframe）列填不满容器
- **现象**：在固定 1400 页里，st.dataframe 列总和 < 容器时，右侧留内部空白；表格内容右边到不了图表/页面的右边线。
- **已确认根因**：glide-data-grid canvas，1.57 下 `use_container_width=True` 与 `width="stretch"` **都不拉伸列**，无 API 可强制。
  - 反例：老版「一元管理1.0」（Streamlit Cloud，更老 streamlit）的 st.dataframe **会**拉伸列填满 → 这是 **Streamlit 版本行为变化**。
- **试过**：① `width:100%`/`overflow:hidden` CSS（无效，反而干扰）② `use_container_width`→`width="stretch"`（无效）③ 改 **HTML 表 `html_table`**（✅ 唯一能填满，已用于 page 05；代价：丢排序/滚动交互，大表性能差）。
- **当前**：page 05 用 html_table（满宽 ✅）；其余 24 页仍 st.dataframe；最新一次又试了 **`width:fit-content + margin:auto`** 想让 st.dataframe「收缩到内容宽 + 居中」（commit `c0f0fac`，**未验证是否破坏 glide，待审查**）。
- **❓ 求审查者**：1.57 有没有可靠让 st.dataframe 列填满或居中的办法？还是只能上 `streamlit-aggrid`（列 flex 可填满，但要加依赖+容器 rebuild+逐页改写 ~24 张表）？

### 5.2 图表（Altair/Vega）绘图区填不满 / 对不齐
- **现象**：图表卡片满宽，但绘图区比表格窄、右边对不齐（曲线图还有右 % 轴占位）。
- **试过且全部失败**：
  - `use_container_width=True`（卡片满宽，绘图区有自然轴边距，不到表格右边）
  - Altair spec `.properties(width="container")`（**不生效**——疑似全局 `zoom:0.90` 干扰 Vega 的容器宽度测量；叠加图 `alt.layer` 对 width=container 也不灵）
  - `width="container"` + `use_container_width` 同时（**冲突**，图反而更窄）
  - CSS `display:flex`/`align-items:center` 居中（无效，外层是 100% 宽 div）
  - CSS `inline-block + margin:auto`（**把绘图区左右裁切了**）
  - 最终回退到 `use_container_width=True` + 卡片 `text-align:center`（安全，不裁切；能否真居中取决于 Vega wrapper 是否 inline-block）
- **结构性结论**：曲线图是**双轴**（左 ¥ + 右 毛利率%），右轴天然占右侧，绘图数据区注定到不了卡片右边——不删双轴去不掉。
- **❓ 求审查者**：在 `zoom:0.90` 环境下，有没有可靠让 Vega 图绘图区填满/居中、且不裁切的方法？是否值得为对齐去掉全局 zoom 或改双轴设计？

### 5.3 其他潜在可疑点（请一并审查）
- **`zoom:0.90`** 是非标准 CSS，可能影响 Vega 测量、getBoundingClientRect 类逻辑——是否该改用 `transform:scale` 或干脆去掉？
- **大量 `!important`**：theme.py 几乎全 `!important`（因 Streamlit 内联样式优先级高）。是否有更干净的写法？
- **`p, span, label, div { color:#475569 !important }`**：把全站正文强制中灰，可能压低某些文字对比度（WCAG）。
- **日文字形用中文字（SC 排 JP 前）**：日文页是否需要 `:lang(ja)` 单独让 JP 优先？
- **侧栏分组标题 UPPERCASE + letter-spacing**：中文/日文大写无意义，是否该去掉。

---

## 6. 本轮全部改动（commit 时间倒序）

```
c0f0fac st.dataframe fit-content+margin auto 居中(测试·glide敏感)
6aaa5a4 移除 02/03/04/11 的 max-width:100% 覆盖(全站统一固定1400居中)
eff5324 图表只用 text-align:center 安全居中(不裁切)
a8ae739 撤销图表居中CSS(导致绘图区裁切)·恢复完整渲染
2971f84 图表改用 text-align:center 居中(flex 不生效)
7aff521 图表绘图内容在卡片内水平居中(填不满两侧对称留白)
091b532 内容区固定宽 1400px 居中
c089457 图表回退原始 use_container_width=True(width=container 不生效)
f24fac7 图表去掉 use_container_width(与 width=container 冲突)
1f40add 店铺毛利 曲线图/柱状图 加 width=container(失败)
27102b4 店铺毛利整页表格改 html_table 满宽
9a62ea6 图表卡片横向 padding 20→6px + 日表回退 st.dataframe
3f5341b 去掉 stDataFrame overflow:hidden/width:100%
6c4803a html_table 助手 + 店铺毛利日表样例
f3f3894 店铺毛利 st.dataframe use_container_width→width=stretch(验证)
7b7090b st.dataframe 强制 width:100%(后证无效)
7587476 侧栏 336→280px + 内容留白 1.5rem
7cdfd7b 整体缩放 0.95→0.90
fd38407 内容上限 1440→1760(后改 1400 固定)
c4a703f 侧边导航 16→17px, tab 13→14px
4ad7836 expander 标题 13px
c8285a0 字号微调批次(语言切换器12/卡片标签12/导航16/tab13/输入居中)
f2982d6 caption 12px + tab 粗体 700
87c8a54 输入框/文本域字号统一 14px
e13eaab 下拉框显示值与菜单选项字号统一 14px
c55325d KPI数字 24px + tabular-nums
1445b78 修图标重叠(去[class*=st-]大锤) + KPI数字 28→20px 可换行
46e19a7 本地打包字体 Inter+Noto SC/JP + 修图标/导航
（更早：主题从 SMIKIE红/Apple 切到 Modern White 蓝；NST 上传模板等非 UI 改动）
```

---

## 7. 给审查者的核心问题（按优先级）

1. **st.dataframe 列填满/居中**：1.57 有无可靠原生办法？还是必须 aggrid / HTML 表？最新的 `width:fit-content` 居中是否安全？
2. **Vega 图表绘图区对齐**：`zoom:0.90` 下如何安全填满/居中不裁切？是否该去 zoom / 改双轴？
3. **整体取舍**：「图表绘图区与表格内容像素级右对齐」是否值得继续追？还是接受图表自然轴边距即可？
4. **代码质量**：满屏 `!important`、全局正文强制灰、日文字形问题，有无更优写法？
5. **字号体系**：上方字号表是否协调？哪些该统一/调整？

> 仓库相关文件：`shared/theme.py`（核心）· `.streamlit/config.toml` · `shared/i18n.py`（语言切换器 + 侧栏导航 CSS）· `shared/auth.py`（紧凑 layout CSS + APP_VERSION）· `pages/05_🏪_店铺别毛利.py`（唯一用 html_table 的页，可作 HTML 表参考）。
