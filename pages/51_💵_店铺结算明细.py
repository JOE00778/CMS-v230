"""店铺结算明细 · 店铺扣减/拨款明细/未结算·损失。
本体は shared/shop_margin/（common.py = 月/市場フィルタ・KPI・CM 素材、tabs_settle.py = 本ページのタブ）。
Boss 2026-11-20「太重了，分开」: 12 タブを 3 ページに分割し、開いたページのタブだけクエリする。"""
import runpy
from pathlib import Path

_D = Path(__file__).resolve().parent.parent / "shared" / "shop_margin"
_g = runpy.run_path(str(_D / "common.py"),
                    init_globals={"PAGE_TITLE": "店铺结算明细", "PAGE_ICON": "💵", "SHOW_KPI": False})
runpy.run_path(str(_D / "tabs_settle.py"), init_globals=_g)
