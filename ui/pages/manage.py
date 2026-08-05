"""股票池管理页（issue #42 从 app.py 拆出；#37 批量增删）"""

import streamlit as st
import pandas as pd
from data.stock_list import search_stocks

from config.pool import (
    load_stock_pool, add_stock, remove_stock, add_stocks, remove_stocks,
)


def page_manage():
    st.title("A股港股量化分析小程序")
    st.subheader("股票池管理")

    pool = load_stock_pool()
    pool_codes = {s["code"] for s in pool}

    col_left, col_right = st.columns([1, 1], gap="large")

    # ========== 左侧：当前股票池列表 ==========
    with col_left:
        st.markdown("### 当前股票池")

        idx_pool = [s for s in pool if s.get("type") == "index"]
        sector_pool = [s for s in pool if s.get("type") == "sector"]
        a_pool = [s for s in pool if s.get("market", "A") == "A" and s.get("type") not in ("index", "sector")]
        hk_pool = [s for s in pool if s.get("market") == "HK"]

        def _render_pool_rows(sub_pool):
            for s in sub_pool:
                c1, c2 = st.columns([4, 1])
                c1.write(f"{s['name']}（{s['code']}）")
                stype = s.get("type", "stock")
                if c2.button("移除", key=f"del_{s['code']}_{stype}"):
                    remove_stock(s["code"], stock_type=stype)
                    st.rerun()

        for label, sub_pool in [("指数", idx_pool), ("板块", sector_pool), ("A股", a_pool), ("港股", hk_pool)]:
            if not sub_pool:
                continue
            # 批量加板块后这一组可能有几百项，全平铺会把页面撑满（#37）
            if len(sub_pool) > 20:
                with st.expander(f"**{label}**（{len(sub_pool)} 只，展开逐个移除）"):
                    _render_pool_rows(sub_pool)
            else:
                st.markdown(f"**{label}**（{len(sub_pool)} 只）")
                _render_pool_rows(sub_pool)

        # 手动输入添加
        st.markdown("---")
        st.markdown("**手动添加**")
        add_cols = st.columns([2, 2, 1, 1, 1])
        new_code = add_cols[0].text_input("代码", placeholder="600519 或 00700", key="manual_code")
        new_name = add_cols[1].text_input("名称", placeholder="贵州茅台", key="manual_name")
        new_market = add_cols[2].selectbox("市场", ["A", "HK"], key="manual_market")
        _type_labels = {"stock": "个股", "index": "指数", "sector": "板块"}
        new_type = add_cols[3].selectbox("类型", ["stock", "index", "sector"], key="manual_type",
                                          format_func=lambda x: _type_labels[x])
        new_kind = None
        if new_type == "sector":
            new_kind = st.selectbox("板块类型", ["industry", "concept"], key="manual_kind",
                                    format_func=lambda x: "行业板块" if x == "industry" else "概念板块")
            st.caption("板块「代码」填同花顺板块代码（行业如 881121、概念如 300733），「名称」填板块全名（akshare 取数 key）。建议用右侧「添加板块」选择。")
        if add_cols[4].button("添加", key="manual_add"):
            if new_code and new_name:
                if add_stock(new_code.strip(), new_name.strip(), new_market,
                             stock_type=new_type, sector_kind=new_kind):
                    st.rerun()
                else:
                    st.warning(f"{new_code} 已在股票池中")
            else:
                st.warning("请填写代码和名称")

    # ========== 右侧：搜索全市场股票 ==========
    with col_right:
        st.markdown("### 搜索全市场股票")

        search_cols = st.columns([3, 1])
        keyword = search_cols[0].text_input("输入代码或名称", placeholder="如: 茅台、00700、银行", key="search_kw")
        market_filter = search_cols[1].selectbox("市场", ["全部", "A股", "港股"], key="search_market")

        if keyword:
            m = None
            if market_filter == "A股":
                m = "A"
            elif market_filter == "港股":
                m = "HK"

            results = search_stocks(keyword, market=m)

            if not results:
                st.info(f"未找到「{keyword}」相关股票。如果是首次使用，列表需加载约 10 秒。")
            else:
                st.markdown(f"找到 **{len(results)}** 只（最多 50 条）")

                # 表头
                h1, h2, h3, h4 = st.columns([2, 2, 1, 1])
                h1.markdown("**代码**")
                h2.markdown("**名称**")
                h3.markdown("**市场**")
                h4.markdown("**操作**")

                for s in results:
                    market_tag = "港股" if s.get("market") == "HK" else "A股"
                    in_pool = s["code"] in pool_codes
                    c1, c2, c3, c4 = st.columns([2, 2, 1, 1])
                    c1.write(s["code"])
                    c2.write(s["name"])
                    c3.write(market_tag)
                    if in_pool:
                        c4.markdown('<span style="color:#27ae60">✓ 已添加</span>', unsafe_allow_html=True)
                    else:
                        if c4.button("添加", key=f"add_{s['code']}"):
                            add_stock(s["code"], s["name"], s.get("market", "A"))
                            st.rerun()
        else:
            st.info("输入关键词搜索 A 股（约 5200 只）或港股（30 只热门），回车确认。")

        # ---- 板块选择器（东财行业/概念板块全量列表，下拉直接选）----
        st.markdown("---")
        st.markdown("### 添加板块")
        from data.sector_list import get_sector_list

        sec_cols = st.columns([3, 1])
        kind_label = {"industry": "行业", "concept": "概念"}
        kind = sec_cols[0].radio("板块类型", ["industry", "concept"], horizontal=True,
                                 format_func=lambda k: kind_label[k] + "板块", key="sec_kind_filter")
        force_sec = sec_cols[1].button("刷新列表", key="refresh_sectors")

        all_sectors = get_sector_list(force_refresh=force_sec)
        subset = [s for s in all_sectors if s["kind"] == kind]

        if not all_sectors:
            st.info("板块列表暂不可用：需从东方财富拉取板块清单（首次约 10 秒），接口异常时为空。"
                    "点「刷新列表」重试，或等东财恢复后再试。")
        elif not subset:
            st.info(f"暂无{kind_label[kind]}板块数据，点「刷新列表」重试。")
        else:
            in_pool_codes = {p["code"] for p in pool if p.get("type") == "sector"}
            not_in_pool = [s for s in subset if s["code"] not in in_pool_codes]
            st.caption(f"{kind_label[kind]}板块共 {len(subset)} 个，其中 {len(not_in_pool)} 个还没加进池子"
                       "（多选框可输入关键字过滤，可一次选多个）")

            # ---- 多选批量添加（#37）----
            options = {f"{s['name']}（{s['code']}）": s for s in not_in_pool}
            picked = st.multiselect("选择板块（可多选）", list(options.keys()), key="sector_pick_multi")
            pick_cols = st.columns([1, 1, 2])
            if pick_cols[0].button(f"添加所选（{len(picked)}）", key="add_picked_sectors",
                                   disabled=not picked):
                n = add_stocks([{"code": options[p]["code"], "name": options[p]["name"],
                                 "market": "A", "type": "sector",
                                 "sector_kind": options[p]["kind"]} for p in picked])
                st.success(f"已添加 {n} 个{kind_label[kind]}板块")
                st.rerun()
            if pick_cols[1].button(f"添加全部{kind_label[kind]}板块（{len(not_in_pool)}）",
                                   key="add_all_sectors", disabled=not not_in_pool):
                n = add_stocks([{"code": s["code"], "name": s["name"], "market": "A",
                                 "type": "sector", "sector_kind": s["kind"]}
                                for s in not_in_pool])
                st.success(f"已添加 {n} 个{kind_label[kind]}板块")
                st.rerun()
            same_kind_in_pool = [p for p in pool if p.get("type") == "sector"
                                 and p.get("sector_kind", "industry") == kind]
            if pick_cols[2].button(f"移除池内全部{kind_label[kind]}板块（{len(same_kind_in_pool)}）",
                                   key="del_all_sectors", disabled=not same_kind_in_pool):
                n = remove_stocks([(p["code"], "sector") for p in same_kind_in_pool])
                st.success(f"已移除 {n} 个{kind_label[kind]}板块")
                st.rerun()

            # 批量加板块的代价必须写在按钮边上：板块没有批量行情接口，总览页要逐个取 THS 快照
            st.caption("⚠️ 板块加进池子就会进「全局信号总览」，而板块**没有批量行情接口**，"
                       "总览页每次刷新要给每个板块单独取一次同花顺快照（实测行业 0.7~1.1s、概念 3.9s，"
                       "60 秒内复用）。参考：10 个板块秒级；90 个（全部行业）约 1~2 分钟；"
                       "465 个（行业+概念）约 5~20 分钟。板块分析只需要少量板块的话，别全量加。")

            # 全量清单（可滚动浏览，不知道名字时直接翻）
            with st.expander(f"展开浏览全部 {kind_label[kind]}板块清单"):
                st.dataframe(
                    pd.DataFrame([{"名称": x["name"], "代码": x["code"],
                                   "在池内": "✓" if x["code"] in in_pool_codes else ""}
                                  for x in subset]),
                    use_container_width=True, hide_index=True,
                )
        st.caption("板块来自同花顺行业/概念板块。添加后出现在概览页「板块」分类与「板块分析」页，可点详情做信号/回测分析。")

