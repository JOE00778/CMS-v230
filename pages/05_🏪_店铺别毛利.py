"""模块 #6 店铺毛利 · 日次推移/负责人/店铺/市场/TOP SKU/价格预警。
本体は shared/shop_margin/（common.py = 月/市場フィルタ・KPI・CM 素材、tabs_main.py = 本ページのタブ）。
Boss 2026-11-20「太重了，分开」: 12 タブを 3 ページに分割し、開いたページのタブだけクエリする。"""
import runpy
from pathlib import Path

_D = Path(__file__).resolve().parent.parent / "shared" / "shop_margin"
_g = runpy.run_path(str(_D / "common.py"),
                    init_globals={"PAGE_TITLE": "店铺毛利", "PAGE_ICON": "🏪"})
runpy.run_path(str(_D / "tabs_main.py"), init_globals=_g)
