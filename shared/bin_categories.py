"""弁天倉庫 bin 用途分类 · 常量定义（Boss 2026-05-27）.

实际的「哪些棚号属于哪个类别」由 PG 表 nst.bin_category 维护，
CMS page33 第 3 tab「🏷️ 货架用途指定」可编辑。本文件只提供:
  - 类别枚举 CATEGORIES（顺序固定: 输出 / 输出中国 / 返品 / 不良品）
  - 优先级 SQL fragment（page33 SQL CASE 直接展开）

类别判定 CASE 优先级: 返品 > 不良品 > 输出中国 > 输出（通常）。
未在 nst.bin_category 出现的棚号 → 默认归通常输出。
"""
from __future__ import annotations

CATEGORIES = ("输出", "输出中国", "返品", "不良品")
