"""共用 fixture。"""
from __future__ import annotations

from unittest import mock

import pytest


@pytest.fixture
def page15_offline():
    """page15 を AppTest で回すとき、外への通信（網調べ・画像検索・斑马 API）を全部止める。

    戻り = register_one の MagicMock（呼ばれた行を検査する用）。既定は全件 ok。
    """
    from shared import banma_client, banma_register, image_search, jan_web
    reg = mock.MagicMock(return_value={"status": "ok", "sku_id": "1", "spu_id": "2", "message": ""})
    with mock.patch.object(jan_web, "fetch_weight", return_value=None), \
            mock.patch.object(jan_web, "rakuten_meta",
                              side_effect=lambda j: jan_web.JanMeta(j, "", "", "", "not_found", source="rakuten")), \
            mock.patch.object(image_search, "_fetch_image_for_jan",
                              return_value=("not_found", None, 0, "offline", None, "kakaku")), \
            mock.patch.object(banma_client.BanmaClient, "from_env",
                              return_value=mock.MagicMock(**{"call.return_value": {"SKUs": []}})), \
            mock.patch.object(banma_client, "ensure_token"), \
            mock.patch.object(banma_register, "register_one", reg):
        yield reg
