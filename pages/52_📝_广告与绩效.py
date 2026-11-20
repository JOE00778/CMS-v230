"""广告费与运营绩效 · 广告费录入/运营绩效。
本体は shared/shop_margin/（common.py = 月/市場フィルタ・KPI・CM 素材、tabs_ads.py = 本ページのタブ）。
Boss 2026-11-20「太重了，分开」: 12 タブを 3 ページに分割し、開いたページのタブだけクエリする。"""
import runpy
from pathlib import Path

_D = Path(__file__).resolve().parent.parent / "shared" / "shop_margin"
_g = runpy.run_path(str(_D / "common.py"),
                    init_globals={"PAGE_TITLE": "广告与绩效", "PAGE_ICON": "📝", "SHOW_KPI": False})
runpy.run_path(str(_D / "tabs_ads.py"), init_globals=_g)
