"""退款明细（出货后未入账订单 · FF-3 暂放）。
本体は shared/shop_margin/（common.py = 月/市場フィルタ・KPI・CM 素材、tabs_refund.py = 本ページのタブ）。
Boss 2026-11-20「太重了，分开」: 12 タブを 3 ページに分割し、開いたページのタブだけクエリする。"""
import runpy
from pathlib import Path

_D = Path(__file__).resolve().parent.parent / "shared" / "shop_margin"
_g = runpy.run_path(str(_D / "common.py"),
                    init_globals={"PAGE_TITLE": "退款明细", "PAGE_ICON": "💸", "SHOW_KPI": False})
runpy.run_path(str(_D / "tabs_refund.py"), init_globals=_g)
