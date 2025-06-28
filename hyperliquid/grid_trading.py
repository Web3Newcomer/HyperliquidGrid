import logging
import eth_account
from eth_account.signers.local import LocalAccount
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
import time
from collections import defaultdict
from threading import Thread
import json

logger = logging.getLogger(__name__)


def setup(base_url=None, skip_ws=False, private_key="", address=""):
    logger.info("Connecting account...")
    account: LocalAccount = eth_account.Account.from_key(private_key)
    if not address:
        address = account.address
    logger.info(f"Running with address: {address}")
    info = Info(base_url, skip_ws)
    spot_user_state = info.spot_user_state(address)
    logger.info(f"Spot balances: {spot_user_state['balances']}")
    if not any(float(b['total']) > 0 for b in spot_user_state["balances"]):
        raise Exception("No spot balance found.")
    exchange = Exchange(account, base_url, account_address=address)
    return address, info, exchange


class GridTrading:
    def __init__(self, address, info, exchange, COIN, gridnum, gridmax, gridmin, tp, eachgridamount, hasspot=False, total_invest=None, price_step=None, grid_ratio=None, centered=False, take_profit=None, stop_loss=None, enable_long_grid=True, enable_short_grid=False, risk_config_path="grid_risk_config.json"):
        self.address = address
        self.info = info
        self.exchange = exchange
        self.COIN = COIN
        self.symbol = self.COIN
        self.gridnum = gridnum
        self.gridmax = gridmax
        self.gridmin = gridmin
        self.tp = tp
        self.eachgridamount = round(eachgridamount, 6)  # 限制精度，避免float_to_wire舍入错误
        self.hasspot = hasspot
        self.total_invest = total_invest
        self.grid_ratio = grid_ratio
        self.centered = centered
        self.take_profit = take_profit  # 百分比，如0.05表示5%
        self.stop_loss = stop_loss      # 百分比，如0.05表示5%
        self.enable_long_grid = enable_long_grid   # 是否启用做多网格
        self.enable_short_grid = enable_short_grid # 是否启用做空网格

        # 获取真实的tick_size
        self.tick_size = self.get_tick_size(self.COIN)
        logger.info(f"获取到 {self.COIN} 的 tick_size: {self.tick_size}")
        
        self.eachprice = []
        self.buy_orders = []
        self.sell_orders = []
        self.filled_buy_oids = set()
        self.filled_sell_oids = set()
        # 做空网格相关
        self.short_orders = []  # 做空网格订单
        self.short_cover_orders = []  # 做空减仓订单
        self.filled_short_oids = set()
        self.filled_short_cover_oids = set()
        self.stats = defaultdict(float)
        self.stats['buy_count'] = 0
        self.stats['sell_count'] = 0
        self.stats['short_count'] = 0
        self.stats['short_cover_count'] = 0
        self.stats['buy_volume'] = 0.0
        self.stats['sell_volume'] = 0.0
        self.stats['short_volume'] = 0.0
        self.stats['short_cover_volume'] = 0.0
        self.stats['realized_pnl'] = 0.0
        self.stats['unrealized_pnl'] = 0.0
        self.stats['last_log_time'] = time.time()
        self.ws_midprice = None
        self.pending_orders_to_place = [] # 存储待补充的订单
        self._start_ws_thread()
        # 确定模式描述
        if self.enable_long_grid and self.enable_short_grid:
            mode_desc = "双向网格"
        elif self.enable_long_grid:
            mode_desc = "只做多网格"
        elif self.enable_short_grid:
            mode_desc = "只做空网格"
        else:
            mode_desc = "无网格模式"
            
        if self.enable_long_grid and self.enable_short_grid:
            raise ValueError("错误：不允许同时启用做多和做空网格（双向网格模式）。请在配置文件中只选择一种模式。")

        logger.info(f"当前模式: {mode_desc}, 止盈: {self.take_profit}, 止损: {self.stop_loss}")

        self.load_risk_config(risk_config_path)

    def _start_ws_thread(self):
        def ws_callback(data):
            if isinstance(data, dict) and 'mid' in data:
                try:
                    self.ws_midprice = float(data['mid'])
                except Exception:
                    pass
            elif isinstance(data, dict) and self.COIN in data:
                try:
                    self.ws_midprice = float(data[self.COIN])
                except Exception:
                    pass
        try:
            sub = {"type": "mids", "coin": self.COIN}
            self.info.subscribe(sub, ws_callback)
            logger.info(f"WebSocket 已订阅 {self.COIN} midprice 实时行情")
        except Exception as e:
            logger.warning(f"WebSocket 订阅 midprice 失败: {e}")

    def get_tick_size(self, coin: str) -> float:
        """从交易所信息中动态获取指定币种的tick_size"""
        try:
            raw_meta = self.info.meta()
            logger.debug(f"API返回的meta数据结构: {list(raw_meta.keys())}")
            
            # 尝试不同的数据结构
            if "universe" in raw_meta and "assetCtxs" in raw_meta:
                universe = raw_meta["universe"]
                asset_index = next((i for i, asset in enumerate(universe) if asset["name"] == coin), None)
                if asset_index is not None:
                    tick_size = float(raw_meta["assetCtxs"][asset_index]["tickSize"])
                    logger.info(f"成功获取 {coin} 的 tick_size: {tick_size}")
                    return tick_size
            
            # 尝试其他可能的数据结构
            if "universe" in raw_meta:
                for asset in raw_meta["universe"]:
                    if asset.get("name") == coin:
                        if "tickSize" in asset:
                            tick_size = float(asset["tickSize"])
                            logger.info(f"从universe中获取 {coin} 的 tick_size: {tick_size}")
                            return tick_size
                        elif "tick_size" in asset:
                            tick_size = float(asset["tick_size"])
                            logger.info(f"从universe中获取 {coin} 的 tick_size: {tick_size}")
                            return tick_size
            
            # 如果都找不到，使用常见币种的默认值
            default_tick_sizes = {
                "BTC": 0.1,
                "ETH": 0.01,
                "SOL": 0.01,
                "HYPE": 0.001,
                "USDC": 0.0001
            }
            
            if coin in default_tick_sizes:
                tick_size = default_tick_sizes[coin]
                logger.warning(f"使用 {coin} 的默认 tick_size: {tick_size}")
                return tick_size
            
            logger.warning(f"无法为 {coin} 找到tick_size，使用默认值 1.0")
            return 1.0
            
        except Exception as e:
            logger.error(f"获取 {coin} 的tick_size时发生错误: {e}")
            logger.debug(f"完整的meta数据: {raw_meta}")
            
            # 错误时也尝试使用默认值
            default_tick_sizes = {
                "BTC": 0.1,
                "ETH": 0.01,
                "SOL": 0.01,
                "HYPE": 0.001,
                "USDC": 0.0001
            }
            
            if coin in default_tick_sizes:
                tick_size = default_tick_sizes[coin]
                logger.warning(f"错误后使用 {coin} 的默认 tick_size: {tick_size}")
                return tick_size
            
            return 1.0

    def round_to_tick_size(self, price: float) -> float:
        """所有挂单价格都四舍五入为整数"""
        return int(round(price))

    def get_midprice(self):
        if hasattr(self, 'ws_midprice') and self.ws_midprice is not None:
            return self.ws_midprice
        else:
            try:
                l2_data = self.info.l2_snapshot(self.COIN)
                levels = l2_data['levels']
                bid = levels[0][0]['px']
                ask = levels[1][0]['px']
                mid = (float(bid) + float(ask)) / 2
                return mid
            except Exception as e:
                logger.warning(f"获取midprice失败: {e}")
                return None

    def get_position(self):
        try:
            user_state = self.info.user_state(self.address)
            positions = user_state.get("assetPositions", [])
            for position in positions:
                item = position["position"]
                if item["coin"] == self.COIN:
                    return float(item["szi"])
            return 0.0
        except Exception as e:
            logger.warning(f"获取持仓异常: {e}")
            return 0.0

    def compute(self):
        midprice = self.get_midprice()
        if not midprice or midprice <= 0:
            logger.error(f"无效的midprice: {midprice}, 无法计算网格")
            return

        # 自动设置网格区间
        if self.gridmin is None or self.gridmax is None:
            # 使用grid_ratio参数，如果没有设置则使用默认值0.1
            grid_ratio = getattr(self, 'grid_ratio', 0.1)
            price_range = midprice * grid_ratio
            self.gridmax = midprice + price_range
            self.gridmin = midprice - price_range
            logger.info(f"自动设置网格区间 gridmin={self.gridmin:.6f}, gridmax={self.gridmax:.6f}, grid_ratio={grid_ratio}")
        
        # 使用更精确的网格价格计算
        pricestep = (self.gridmax - self.gridmin) / self.gridnum
        self.eachprice = []
        
        logger.info(f"计算网格价格: tick_size={self.tick_size}, 价格步长={pricestep}")
        
        for i in range(self.gridnum + 1):
            # 计算原始价格
            raw_price = self.gridmin + i * pricestep
            # 四舍五入到tick_size
            rounded_price = self.round_to_tick_size(raw_price)
            self.eachprice.append(rounded_price)
            logger.debug(f"网格 {i}: {raw_price} -> {rounded_price}")

        logger.info(f"Grid levels: {self.eachprice}")
        
        # 根据当前价格决定挂哪些单
        midprice = self.get_midprice()
        
        # 做多网格初始化
        if self.enable_long_grid:
            for i, price in enumerate(self.eachprice):
                if price < midprice:
                    self.place_order_with_retry(self.COIN, True, self.eachgridamount, price, {"limit": {"tif": "Gtc"}}, i)

        # 做空网格初始化
        if self.enable_short_grid:
            for i, price in enumerate(self.eachprice):
                if price > midprice:
                    self.place_order_with_retry(self.COIN, False, self.eachgridamount, price, {"limit": {"tif": "Gtc"}}, i, is_short_order=True)


    def _find_price_in_order(self, order_dict):
        """递归查找avgPx或limitPx，优先自身查找，再递归'order'字段，再递归所有value，兼容所有主流API结构，支持嵌套dict和list"""
        if isinstance(order_dict, dict):
            # 优先查找自身
            if 'avgPx' in order_dict and order_dict['avgPx'] not in (None, '', 0, '0', '0.0'):
                try:
                    return float(order_dict['avgPx'])
                except Exception:
                    pass
            if 'limitPx' in order_dict and order_dict['limitPx'] not in (None, '', 0, '0', '0.0'):
                try:
                    return float(order_dict['limitPx'])
                except Exception:
                    pass
            # 优先递归'order'字段
            if 'order' in order_dict:
                px = self._find_price_in_order(order_dict['order'])
                if px is not None:
                    return px
            # 再递归所有value
            for v in order_dict.values():
                px = self._find_price_in_order(v)
                if px is not None:
                    return px
        elif isinstance(order_dict, list):
            for item in order_dict:
                px = self._find_price_in_order(item)
                if px is not None:
                    return px
        return None

    def check_orders(self):
        if not hasattr(self, '_last_check_time'):
            self._last_check_time = 0
        current_time = time.time()
        if current_time - self._last_check_time < 5:
            return
        self._last_check_time = current_time
        try:
            open_orders_map = {o['oid']: o for o in self.info.open_orders(self.address)}
        except Exception as e:
            logger.warning(f"获取挂单失败: {e}")
            return
        # 检查做多网格的买单
        if self.enable_long_grid:
            active_buy_oids = {o['oid'] for o in self.buy_orders}
            filled_buy_oids = active_buy_oids - set(open_orders_map.keys())
            for oid in filled_buy_oids:
                buy_order_meta = next((o for o in self.buy_orders if o['oid'] == oid), None)
                if not buy_order_meta: continue
                try:
                    fill_info = self.info.query_order_by_oid(self.address, oid)
                    # 递归查找成交价格
                    buy_price = self._find_price_in_order(fill_info)
                    if buy_price is None:
                        logger.error(f"买单 {oid} 无法确定成交价格，跳过此订单。 Fill info: {fill_info}")
                        continue
                    logger.info(f"🎯 检测到买单成交: oid={oid}, 价格={buy_price}")
                    # 经典循环网格：买单成交，挂出卖单平仓；卖单成交，挂出买单开仓
                    # 在买单成交价之上增加一个固定的止盈价差来挂卖单
                    sell_price = self.round_to_tick_size(buy_price * (1 + self.tp))
                    logger.info(f"【下单决策】买单 {oid} 成交于 {buy_price}。")
                    logger.info(f"【下单决策】根据止盈率 {self.tp}，计算目标卖价: {buy_price} * (1 + {self.tp}) = {buy_price * (1 + self.tp)}")
                    logger.info(f"【下单决策】四舍五入后，最终止盈卖价为: {sell_price}")
                    if sell_price <= buy_price:
                        original_sell_price = sell_price
                        sell_price = self.round_to_tick_size(buy_price + self.tick_size)
                        logger.error(f"【严重警告】计算出的卖价({original_sell_price}) <= 买价({buy_price})。")
                        logger.error(f"为防止亏损，已强制将卖价调整为 {sell_price} (买价 + 一个tick_size)。")
                    logger.info(f"准备挂出平仓卖单: 价格={sell_price}, 数量={self.eachgridamount}")
                    self.place_order_with_retry(self.COIN, False, self.eachgridamount, sell_price, {"limit": {"tif": "Gtc"}, "reduceOnly": True}, buy_order_meta['index'])
                    self.buy_orders = [o for o in self.buy_orders if o['oid'] != oid]
                except Exception as e:
                    logger.error(f"处理买单 {oid} 成交时异常: {e}")

        # 检查做空网格的开仓单
        if self.enable_short_grid:
            active_short_oids = {o['oid'] for o in self.short_orders}
            filled_short_oids = active_short_oids - set(open_orders_map.keys())
            for oid in filled_short_oids:
                short_order_meta = next((o for o in self.short_orders if o['oid'] == oid), None)
                if not short_order_meta: continue

                try:
                    fill_info = self.info.query_order_by_oid(self.address, oid)
                    # 递归查找成交价格
                    short_price = self._find_price_in_order(fill_info)
                    if short_price is None:
                        logger.error(f"做空单 {oid} 无法确定成交价格，跳过此订单。 Fill info: {fill_info}")
                        continue
                    logger.info(f"🎯 检测到做空单成交: oid={oid}, 价格={short_price}")

                    # 移除已成交做空单
                    self.short_orders = [o for o in self.short_orders if o['oid'] != oid]

                    # 挂一个止盈平仓买单
                    cover_price = self.round_to_tick_size(short_price * (1 - self.tp))
                    logger.info(f"做空单成交，挂平仓买单: 价格={cover_price}, 数量={self.eachgridamount}")
                    self.place_order_with_retry(self.COIN, True, self.eachgridamount, cover_price, {"limit": {"tif": "Gtc"}}, short_order_meta['index'], reduce_only=True)

                    # 【修复核心】补充：如果该做空单是补挂的（即由平空单成交后补挂），也要在此处挂出对应的平仓买单
                    # 只要是short_orders列表里的单子，无论初始还是补挂，成交后都要补买单

                except Exception as e:
                    logger.error(f"处理做空单 {oid} 成交时异常: {e}")

        # 检查所有卖单（包括做多网格的平仓单和做空网格的开仓单）
        active_sell_oids = {o['oid'] for o in self.sell_orders}
        filled_sell_oids = active_sell_oids - set(open_orders_map.keys())
        for oid in filled_sell_oids:
            sell_order_meta = next((o for o in self.sell_orders if o['oid'] == oid), None)
            if not sell_order_meta: continue

            try:
                fill_info = self.info.query_order_by_oid(self.address, oid)
                # 递归查找成交价格，兼容所有结构
                sell_price = self._find_price_in_order(fill_info)
                if sell_price is None:
                    logger.error(f"卖单 {oid} 无法确定成交价格，跳过此订单。 Fill info: {fill_info}")
                    continue
                logger.info(f"🎯 检测到卖单成交: oid={oid}, 价格={sell_price}")

                # 移除已成交卖单
                self.sell_orders = [o for o in self.sell_orders if o['oid'] != oid]

                # 根据网格模式决定下一步操作
                if self.enable_long_grid:
                    # 在只做多模式下，卖单是平仓单，成交意味着盈利。
                    # 我们需要在其下方重新挂一个买单，以维持网格密度。
                    buy_price = self.round_to_tick_size(sell_price / (1 + self.tp))
                    if buy_price >= sell_price:
                        original_buy_price = buy_price
                        buy_price = self.round_to_tick_size(sell_price - self.tick_size)
                        logger.error(f"【严重警告】为卖单 {oid} 计算出的新买价({original_buy_price}) >= 卖价({sell_price})。")
                        logger.error(f"为防止亏损，已强制将买价调整为 {buy_price} (卖价 - 一个tick_size)。")
                    # 新增保护：市价<=买单价时跳过补单
                    if not self.should_place_long_order(buy_price):
                        logger.warning(f"市价{self.get_midprice()}<=补买单价{buy_price}，跳过补单，防止刷单。")
                        continue
                    logger.info(f"卖单成交，重新挂买单: 价格={buy_price}, 数量={self.eachgridamount}")
                    self.place_order_with_retry(self.COIN, True, self.eachgridamount, buy_price, {"limit": {"tif": "Gtc"}}, sell_order_meta['index'])
                # 注意：做空网格的开仓单现在在单独的处理逻辑中，这里不再处理
            except Exception as e:
                logger.error(f"处理卖单 {oid} 成交时异常: {e}")

        # 检查做空网格的平仓单（cover a short）
        if self.enable_short_grid:
            active_cover_oids = {o['oid'] for o in self.short_cover_orders}
            filled_cover_oids = active_cover_oids - set(open_orders_map.keys())
            for oid in filled_cover_oids:
                cover_order_meta = next((o for o in self.short_cover_orders if o['oid'] == oid), None)
                if not cover_order_meta: continue

                try:
                    fill_info = self.info.query_order_by_oid(self.address, oid)
                    # 递归查找成交价格（直接传入fill_info，支持多层嵌套）
                    cover_price = self._find_price_in_order(fill_info)
                    if cover_price is None:
                        logger.error(f"平空单 {oid} 无法确定成交价格，跳过此订单。 Fill info: {fill_info}")
                        continue
                    logger.info(f"🎯 检测到平空单成交: oid={oid}, 价格={cover_price}")

                    # 移除已成交平仓单
                    self.short_cover_orders = [o for o in self.short_cover_orders if o['oid'] != oid]
                    # 重新挂一个做空单
                    short_price = self.round_to_tick_size(cover_price / (1 - self.tp))
                    # 新增保护：市价>=补卖单价时跳过补单
                    if not self.should_place_short_order(short_price):
                        logger.warning(f"市价{self.get_midprice()}>=补卖单价{short_price}，跳过补单，防止刷单。")
                        continue
                    logger.info(f"平空单成交，重新挂做空单: 价格={short_price}, 数量={self.eachgridamount}")
                    self.place_order_with_retry(self.COIN, False, self.eachgridamount, short_price, {"limit": {"tif": "Gtc"}}, cover_order_meta['index'])
                except Exception as e:
                    logger.error(f"处理平空单 {oid} 成交时异常: {e}")
                    
        # --- 自动补单闭环：仓位归零且无挂单时自动补挂做空单（加冷却和标志位防止重复） ---
        if self.enable_short_grid:
            if not hasattr(self, '_last_replenish_time'):
                self._last_replenish_time = 0
            if not hasattr(self, '_is_replenishing'):
                self._is_replenishing = False
            pos = self.get_position()
            now = time.time()
            if pos == 0 and not self.short_orders and not self.short_cover_orders:
                if not self._is_replenishing and now - self._last_replenish_time > 60:
                    logger.warning(f"[自动补单] 检测到仓位已归零且无任何做空挂单，自动补挂一组新的做空单...（冷却期已过）")
                    self._is_replenishing = True
                    self.compute()
                    self._last_replenish_time = now
                    self._is_replenishing = False
                elif self._is_replenishing:
                    logger.info("[自动补单] 已在补单中，跳过本次触发。")
                else:
                    logger.info(f"[自动补单] 冷却中，{int(60 - (now - self._last_replenish_time))}秒后可再次补单。")

        # --- 新增：做多网格的仓位归零自动补单闭环 ---
        if self.enable_long_grid:
            if not hasattr(self, '_last_long_replenish_time'):
                self._last_long_replenish_time = 0
            if not hasattr(self, '_is_long_replenishing'):
                self._is_long_replenishing = False
            pos = self.get_position()
            now = time.time()
            if pos == 0 and not self.buy_orders and not self.sell_orders:
                if not self._is_long_replenishing and now - self._last_long_replenish_time > 60:
                    logger.warning(f"[自动补单] 检测到仓位已归零且无任何做多挂单，自动补挂一组新的做多单...（冷却期已过）")
                    self._is_long_replenishing = True
                    self.compute()
                    self._last_long_replenish_time = now
                    self._is_long_replenishing = False
                elif self._is_long_replenishing:
                    logger.info("[自动补单] 已在做多补单中，跳过本次触发。")
                else:
                    logger.info(f"[自动补单] 做多冷却中，{int(60 - (now - self._last_long_replenish_time))}秒后可再次补单。")

    def place_order_with_retry(self, coin, is_buy, sz, px, order_type, grid_index=None, reduce_only=False, is_short_order=False):
        """带重试逻辑的下单函数，处理429限流"""
        max_retries = 5
        logger.info(f"[下单请求] 币种: {coin}, {'买' if is_buy else '卖'}, 数量: {sz}, 价格: {px}, reduceOnly: {reduce_only}, 网格序号: {grid_index}")
        try:
            px = float(px)
            order_result = self.exchange.order(coin, is_buy, sz, px, order_type, reduce_only=reduce_only)
            logger.info(f"[下单响应] 结果: {order_result}")
            if order_result.get("status") == "ok":
                statuses = order_result["response"]["data"].get("statuses", [])
                if statuses:
                    if "resting" in statuses[0]:
                        oid = statuses[0]["resting"]["oid"]
                        logger.info(f"[下单成功] oid: {oid}, 价格: {px}, 数量: {sz}, reduceOnly: {reduce_only}, 网格序号: {grid_index}")
                        if is_buy and not reduce_only:
                            self.buy_orders.append({"index": grid_index, "oid": oid})
                        elif is_buy and reduce_only:
                            self.short_cover_orders.append({"index": grid_index, "oid": oid})
                        elif not is_buy and is_short_order:
                            self.short_orders.append({"index": grid_index, "oid": oid})
                        elif not is_buy and not is_short_order:
                            self.sell_orders.append({"index": grid_index, "oid": oid, "is_tp": reduce_only})
                    elif "filled" in statuses[0]:
                        filled_info = statuses[0]["filled"]
                        logger.info(f"[下单直接成交] oid: {filled_info.get('oid')}, 价格: {filled_info.get('avgPx')}, 数量: {filled_info.get('totalSz')}, reduceOnly: {reduce_only}, 网格序号: {grid_index}")
                        return
                    else:
                        logger.warning(f"[下单异常] 状态异常，将加入重试列表: {statuses}")
                        self.pending_orders_to_place.append({"coin": coin, "is_buy": is_buy, "sz": float(sz), "limit_px": float(px), "order_type": order_type, "original_index": grid_index, "reduce_only": reduce_only})
                else:
                    logger.warning(f"[下单异常] 状态异常，将加入重试列表: {statuses}")
                    self.pending_orders_to_place.append({"coin": coin, "is_buy": is_buy, "sz": float(sz), "limit_px": float(px), "order_type": order_type, "original_index": grid_index, "reduce_only": reduce_only})
            else:
                logger.error(f"[下单失败] 结果: {order_result}，将加入重试列表")
                self.pending_orders_to_place.append({"coin": coin, "is_buy": is_buy, "sz": float(sz), "limit_px": float(px), "order_type": order_type, "original_index": grid_index, "reduce_only": reduce_only})
        except Exception as e:
            logger.error(f"[下单异常] 发生异常: {e}，将加入重试列表")
            self.pending_orders_to_place.append({"coin": coin, "is_buy": is_buy, "sz": float(sz), "limit_px": float(px), "order_type": order_type, "original_index": grid_index, "reduce_only": reduce_only})

    def _retry_pending_orders(self):
        if not self.pending_orders_to_place:
            return
        logger.info(f"🔄 重试 {len(self.pending_orders_to_place)} 个失败订单...")
        for order_info in self.pending_orders_to_place[:]:
            limit_px = float(order_info["limit_px"])
            sz = float(order_info["sz"])
            self.place_order_with_retry(order_info["coin"], order_info["is_buy"], sz, limit_px, order_info["order_type"], order_info["original_index"], order_info["reduce_only"])
            self.pending_orders_to_place.remove(order_info)

    def trader(self):
        self._retry_pending_orders()
        self.check_orders()

    def get_balance(self):
        """获取账户USDC余额，简化实现，实际可根据币种调整"""
        try:
            user_state = self.info.user_state(self.address)
            for asset in user_state.get("spotBalances", []):
                if asset["coin"] == "USDC":
                    return float(asset["total"])
            return 0.0
        except Exception as e:
            logger.warning(f"获取余额异常: {e}")
            return 0.0

    def load_risk_config(self, config_path="grid_risk_config.json"):
        """从配置文件加载风控参数"""
        # 默认参数
        self.risk_config = {
            "enable_rebalance": True,
            "rebalance_interval": 3600,
            "max_pos_factor": 2,
            "min_balance_factor": 1.2,
            "min_order_ratio": 0.8,
            "volatility_window": 60,
            "volatility_threshold": 0.01
        }
        try:
            with open(config_path, "r") as f:
                user_cfg = json.load(f)
                self.risk_config.update(user_cfg)
            logger.info(f"已加载风控配置: {self.risk_config}")
        except Exception as e:
            logger.warning(f"未找到或加载风控配置失败({e})，使用默认风控参数")

    def pre_rebalance_risk_check(self):
        cfg = self.risk_config
        # 1. 持仓风险
        pos = self.get_position()
        max_pos = self.eachgridamount * self.gridnum * cfg.get("max_pos_factor", 2)
        if abs(pos) > max_pos:
            logger.warning(f"[风控] 持仓过大: {pos}, 超过最大允许: {max_pos}")
            return False
        # 2. 余额风险
        balance = self.get_balance()
        min_balance = self.eachgridamount * self.gridnum * cfg.get("min_balance_factor", 1.2)
        if balance < min_balance:
            logger.warning(f"[风控] 余额不足: {balance} < {min_balance}")
            return False
        # 3. 挂单风险
        try:
            open_orders = self.info.open_orders(self.address)
        except Exception as e:
            logger.warning(f"[风控] 查询挂单异常: {e}")
            return False
        expected_orders = self.gridnum
        if len(open_orders) < expected_orders * cfg.get("min_order_ratio", 0.8):
            logger.warning(f"[风控] 挂单数量异常: {len(open_orders)} < 预期~{expected_orders}")
            return False
        # 4. API/网络风险
        if hasattr(self, 'api_error_count') and self.api_error_count > 5:
            logger.warning(f"[风控] API最近失败次数过多")
            return False
        # 5. 极端行情风险
        if self.is_extreme_volatility(cfg.get("volatility_window", 60), cfg.get("volatility_threshold", 0.01)):
            logger.warning(f"[风控] 检测到极端行情，暂停再平衡")
            return False
        return True

    def is_extreme_volatility(self, window=None, threshold=None):
        """判断window秒内价格波动是否超过阈值"""
        cfg = self.risk_config
        window = window if window is not None else cfg.get("volatility_window", 60)
        threshold = threshold if threshold is not None else cfg.get("volatility_threshold", 0.01)
        if not hasattr(self, '_price_history'):
            self._price_history = []
        now = time.time()
        mid = self.get_midprice()
        self._price_history.append((now, mid))
        self._price_history = [(t, p) for t, p in self._price_history if now - t <= window]
        if len(self._price_history) < 2:
            return False
        prices = [p for t, p in self._price_history]
        min_p, max_p = min(prices), max(prices)
        if min_p > 0 and (max_p - min_p) / min_p > threshold:
            return True
        return False

    def rebalance(self):
        """定时再平衡：撤销所有挂单，清空本地挂单状态，重新计算网格并挂单"""
        if not self.pre_rebalance_risk_check():
            logger.warning("[再平衡] 风控不通过，跳过本次再平衡")
            return
        logger.info("[再平衡] 开始撤销所有未成交挂单...")
        try:
            open_orders = self.info.open_orders(self.address)
        except Exception as e:
            logger.error(f"[再平衡] 无法获取当前挂单，跳过本次再平衡: {e}")
            return
        cancel_requests = [{"coin": self.COIN, "oid": o['oid']} for o in open_orders]
        if cancel_requests:
            try:
                self.exchange.bulk_cancel(cancel_requests)
                logger.info(f"[再平衡] 已发送 {len(cancel_requests)} 个撤单请求")
                time.sleep(2)
            except Exception as e:
                logger.error(f"[再平衡] 批量撤单请求异常，跳过本次再平衡: {e}")
                return
        # 清空本地状态
        self.buy_orders.clear()
        self.sell_orders.clear()
        self.filled_buy_oids.clear()
        self.filled_sell_oids.clear()
        self.pending_orders_to_place.clear()
        # 重新计算网格并挂单
        logger.info("[再平衡] 重新计算网格并挂单...")
        self.compute()

    def run(self):
        logger.info("🚀 网格交易策略启动")
        self.compute()
        last_rebalance_time = time.time()
        while True:
            try:
                self.trader()
                # 配置文件控制是否启用再平衡及周期
                if self.risk_config.get("enable_rebalance", True):
                    if time.time() - last_rebalance_time > self.risk_config.get("rebalance_interval", 3600):
                        self.rebalance()
                        last_rebalance_time = time.time()
                time.sleep(5)
            except KeyboardInterrupt:
                logger.info("🛑 用户中断，正在安全退出...")
                try:
                    open_orders = self.info.open_orders(self.address)
                    if open_orders:
                        cancel_requests = [{"coin": self.COIN, "oid": o['oid']} for o in open_orders]
                        self.exchange.bulk_cancel(cancel_requests)
                        logger.info("已撤销所有挂单。")
                except Exception as e:
                    logger.error(f"退出时撤销挂单失败: {e}")
                break
            except Exception as e:
                logger.error(f"❌ 策略运行异常: {e}")
                time.sleep(10)

    def should_place_long_order(self, price):
        mid = self.get_midprice()
        return mid > price

    def should_place_short_order(self, price):
        mid = self.get_midprice()
        return mid < price