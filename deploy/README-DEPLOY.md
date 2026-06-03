# CMS v3 · 生产部署指南

> ⚠️ **本文档已过时（2026-05-08）** —— 描述的是 Streamlit Cloud 部署 SOP，生产已迁至元川さん（Dell Inspiron 5405）Docker bind mount 模式。
>
> 当前生产部署见：[deploy/windows/](windows/) · README + FRESH_INSTALL + MIGRATION
> 推送流程见：根 CLAUDE.md L64
> 部署链路：Mac push → GitHub → **元川さん 手动 git pull**（无 cron · 2026-05-20 确认）
>
> 本文档保留作历史参考，不再维护。

---

# 旧版 · CMS v3 · 生产部署指南（Streamlit Cloud · 已废止）

> Boss 自助部署 · 全程 30 分钟。
> 不需要给 Claudex 任何 GitHub / Supabase 凭证。

---

## 找现有 streamlit.app 关联的 GitHub repo（3 个路径）

### 路径 A · Streamlit Cloud 后台（最准）
1. 打开 https://share.streamlit.io
2. 登录后看 **Workspaces / My apps**
3. 找到 `order-management-app`
4. 点 ⋯ 菜单 → **Settings** 或 **Manage app**
5. 看 **Repository** 字段：那一行就是 GitHub URL

### 路径 B · 自己 GitHub 账号
1. 打开 https://github.com/?tab=repositories
2. 按 **Recently updated** 排序
3. 找带 "order" / "shouhin" / "streamlit" / "商品" 字样的 repo

### 路径 C · 新建 repo 重新部署（最简单）⭐ 推荐

如果 5/02 当时是直接发源码不是 git，那现在确实没 repo。**新建一个 repo 重新部署**：

1. 打开 https://github.com/new
2. Repository name: `shouhin-jouhou-kanri-v3`（或任意）
3. Public / Private 都行
4. **Create repository**
5. 按下面步骤把代码 push 进去 + 部署到 Streamlit Cloud

---

## 自助部署步骤（推荐 · 30 分钟）

### Step 1 · 在本机准备代码

```bash
cd ~/CC/CMS

# 检查需要的所有文件齐全
ls pages/ modules/ shared/ data_warehouse/

# 跟 Boss 之前的源码合并（保留旧 mode 路由）
cp legacy_streamlit/main.py cms.py.bak  # 备份
# 注：cms.py 是新主入口，main.py 是旧 mode 路由（保留 backward compat）
```

### Step 2 · 创建 .streamlit/secrets.toml（**不 commit · 在 Streamlit Cloud 后台填**）

```toml
# 后端切换（dev=本地 SQLite / prod=Supabase）
BACKEND = "supabase"

# Supabase 凭证
SUPABASE_URL = "https://YOUR_PROJECT.supabase.co"
SUPABASE_KEY = "eyJhbGc..."

# 上传密码（保留旧值）
UPLOAD_PASSWORD = "pass1234"

# Lark（如已配）
LARK_APP_ID = "cli_xxx"
LARK_APP_SECRET = "xxx"
LARK_SPREADSHEET_TOKEN = "xxx"
LARK_SHEET_ID = "xxx"
```

### Step 3 · git init + push

```bash
cd ~/CC/CMS
git init
git add .
git commit -m "feat: v3 商品信息管理增强（11 page + 3 模块 + 5 ingestor）"
git branch -M main
git remote add origin https://github.com/<你的用户名>/<repo 名>.git
git push -u origin main
```

### Step 4 · Streamlit Cloud 部署

#### 选项 A · 已有 app（找到了路径 A 的 repo URL）
1. https://share.streamlit.io → 你的 app
2. **Settings → Secrets**：把上面 secrets.toml 内容粘进去
3. **Reboot app**

#### 选项 B · 新 repo 部署（路径 C）
1. https://share.streamlit.io
2. **New app** → 选 GitHub repo + branch=main
3. **Main file**: `cms.py`
4. **Advanced settings → Secrets**：粘 secrets.toml 内容
5. **Deploy**

### Step 5 · Supabase schema 迁移

```bash
# 在本地把 schema 导出
cd ~/CC/CMS
sqlite3 data_warehouse/warehouse.db .dump > /tmp/schema_v3.sql

# 提取仅 CREATE TABLE 语句
grep -E "CREATE TABLE|CREATE INDEX" /tmp/schema_v3.sql > /tmp/schema_v3_ddl.sql

# 在 Supabase Dashboard → SQL Editor 粘贴执行
# 把 SQLite 的 INTEGER PRIMARY KEY AUTOINCREMENT 改为 SERIAL PRIMARY KEY
# 把 TIMESTAMP DEFAULT CURRENT_TIMESTAMP 改为 TIMESTAMPTZ DEFAULT NOW()
```

或者更简单：让 Streamlit 启动时自动 migrate（已实现 `init_db`）。

### Step 6 · 4 月份数据迁移（可选）

```bash
# 本地 SQLite → CSV
cd ~/CC/CMS
for table in item_master item_master_netsuite supplier_cost supply_cycle \
             nst_inventory_snapshot nst_store_sales nst_turnover \
             shopee_orders shopee_payouts shopee_fees; do
  sqlite3 data_warehouse/warehouse.db ".mode csv" ".headers on" \
    "SELECT * FROM $table" > /tmp/$table.csv
done

# 在 Supabase Dashboard → Table Editor → 每张表 Import CSV
```

---

## 文件清单（全部 push 到 repo）

```
shouhin-jouhou-kanri-v3/
├── cms.py                  # 主入口
├── requirements.txt                # streamlit / pandas / plotly / supabase / openpyxl
├── .streamlit/
│   ├── config.toml                # 主题配置
│   └── secrets.toml.example       # 密钥模板
├── pages/                         # 12 个新 page（02-99）
├── modules/                       # 4 个 modules
│   ├── cost_sync/
│   ├── inventory_health/
│   ├── rank_classifier/
│   └── operation_advice/
├── data_warehouse/
│   ├── templates/                # NST/JD item master 模板（page 03/07/15 用）
│   └── warehouse.db              # 本地缓存（gitignore）
├── shared/
│   ├── db.py
│   ├── supabase_client.py
│   └── xml_xls.py
├── docs/                          # 4 个设计文档
└── README.md
```

`.gitignore` 添加：
```
.venv/
__pycache__/
*.pyc
data_warehouse/warehouse.db
.streamlit/secrets.toml
```

---

## 部署完成后验证

1. 打开 streamlit.app URL
2. 侧边栏看到 12 个 page
3. 任选 page 06 / 07 / 11 → 数据从 Supabase 拉
4. 测试 page 13 改廃確認 → Boss 三按钮点击 → 写 Supabase

---

## 我可以替 Boss 做的（不需要凭证）

1. **代码完全准备好**（已完成）
2. **写完整 push 命令脚本**（下面 deploy/push.sh）
3. **生成 Supabase schema 适配版**（PostgreSQL 语法）
4. **生成 4 月份数据 CSV 包**

Boss 只需 3 步：
1. 新建 GitHub repo（点 5 下）
2. 跑 `bash deploy/push.sh <repo-url>`
3. Streamlit Cloud → New app → 填 secrets → Deploy

---

## ⚠️ Boss 待答 1 件事

**部署到哪里？**
- 选项 1 · 找到现有 repo（路径 A）→ 我帮 commit + 改 push 脚本
- 选项 2 · 新建 repo（路径 C，推荐）→ 我已经准备好了
- 选项 3 · Boss 自己手动操作 → 看上面 Step 1-6
