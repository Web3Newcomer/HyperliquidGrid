import logging
import eth_account
from eth_account.signers.local import LocalAccount
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
import time
from collections import defaultdict
from threading import Thread

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
    def __init__(self, address, info, exchange, COIN, gridnum, gridmax, gridmin, tp, eachgridamount, hasspot=False, total_invest=None, price_step=None, grid_ratio=None, centered=False, take_profit=None, stop_loss=None, enable_long_grid=True, enable_short_grid=False):
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
        """将价格四舍五入到最接近的tick_size"""
        if self.tick_size == 0: 
            return round(price, 8)
        
        # 简单可靠的方法：先除以tick_size，四舍五入到整数，再乘以tick_size
        steps = round(price / self.tick_size)
        result = steps * self.tick_size
        
        # 确保结果的小数位数不超过tick_size的小数位数
        tick_decimals = len(str(self.tick_size).split('.')[-1]) if '.' in str(self.tick_size) else 0
        return round(result, tick_decimals)

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
            price_range = midprice * 0.1 # 默认范围10%
            self.gridmax = midprice + price_range
            self.gridmin = midprice - price_range
            logger.info(f"自动设置网格区间 gridmin={self.gridmin:.6f}, gridmax={self.gridmax:.6f}")
        
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
                    self.place_order_with_retry(self.COIN, False, self.eachgridamount, price, {"limit": {"tif": "Gtc"}}, i)


    def check_orders(self):
        if not hasattr(self, '_last_check_time'):
            self._last_check_time = 0
        
        current_time = time.time()
        # 增加检查间隔到5秒，减少API调用频率
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
                    
                    # 增加健壮性检查，确保订单状态和信息完整
                    if fill_info.get("status") == "filled" or (isinstance(fill_info.get("order"), dict) and fill_info["order"].get("status") == "filled"):
                        order_details = fill_info.get("order", {})
                        
                        # 安全地获取成交价格，如果 'avgPx' 不存在，则使用挂单价 'limitPx'
                        if 'avgPx' in order_details and float(order_details['avgPx']) > 0:
                            buy_price = float(order_details['avgPx'])
                        elif 'limitPx' in order_details:
                            buy_price = float(order_details['limitPx'])
                            logger.warning(f"买单 {oid} 无法获取 avgPx，使用 limitPx {buy_price} 作为成交价。")
                        else:
                            logger.error(f"买单 {oid} 无法确定成交价格，跳过此订单。 Fill info: {fill_info}")
                            continue

                        logger.info(f"🎯 检测到买单成交: oid={oid}, 价格={buy_price}")

                        # 经典循环网格：买单成交，挂出卖单平仓；卖单成交，挂出买单开仓
                        # 在买单成交价之上增加一个固定的止盈价差来挂卖单
                        sell_price = self.round_to_tick_size(buy_price * (1 + self.tp))

                        if sell_price <= buy_price:
                            original_sell_price = sell_price
                            sell_price = self.round_to_tick_size(buy_price + self.tick_size)
                            logger.error(f"【严重警告】计算出的卖价({original_sell_price}) <= 买价({buy_price})。")
                            logger.error(f"为防止亏损，已强制将卖价调整为 {sell_price} (买价 + 一个tick_size)。")

                        logger.info(f"准备挂出平仓卖单: 价格={sell_price}, 数量={self.eachgridamount}")
                        self.place_order_with_retry(self.COIN, False, self.eachgridamount, sell_price, {"limit": {"tif": "Gtc"}, "reduceOnly": True}, buy_order_meta['index'])

                        # 从活动列表中移除已成交的买单
                        self.buy_orders = [o for o in self.buy_orders if o['oid'] != oid]
                    else:
                        logger.info(f"订单 {oid} 状态不是 'filled' 或信息不完整，跳过。状态: {fill_info.get('status')}")

                except Exception as e:
                    logger.error(f"处理买单 {oid} 成交时异常: {e}")

        # 检查所有卖单（包括做多网格的平仓单和做空网格的开仓单）
        active_sell_oids = {o['oid'] for o in self.sell_orders}
        filled_sell_oids = active_sell_oids - set(open_orders_map.keys())
        for oid in filled_sell_oids:
            sell_order_meta = next((o for o in self.sell_orders if o['oid'] == oid), None)
            if not sell_order_meta: continue

            try:
                fill_info = self.info.query_order_by_oid(self.address, oid)

                if fill_info.get("status") == "filled" or (isinstance(fill_info.get("order"), dict) and fill_info["order"].get("status") == "filled"):
                    order_details = fill_info.get("order", {})

                    if 'avgPx' in order_details and float(order_details['avgPx']) > 0:
                        sell_price = float(order_details['avgPx'])
                    elif 'limitPx' in order_details:
                        sell_price = float(order_details['limitPx'])
                        logger.warning(f"卖单 {oid} 无法获取 avgPx，使用 limitPx {sell_price} 作为成交价。")
                    else:
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

                        logger.info(f"卖单成交，重新挂买单: 价格={buy_price}, 数量={self.eachgridamount}")
                        self.place_order_with_retry(self.COIN, True, self.eachgridamount, buy_price, {"limit": {"tif": "Gtc"}}, sell_order_meta['index'])

                    elif self.enable_short_grid:
                        # 在只做空模式下，卖单是开仓单
                        # 挂一个止盈平仓单（买入）
                        cover_price = self.round_to_tick_size(sell_price * (1 - self.tp))
                        logger.info(f"做空单成交，挂平仓买单: 价格={cover_price}, 数量={self.eachgridamount}")
                        self.place_order_with_retry(self.COIN, True, self.eachgridamount, cover_price, {"limit": {"tif": "Gtc"}, "reduceOnly": True}, sell_order_meta['index'])
                else:
                    logger.info(f"订单 {oid} 状态不是 'filled' 或信息不完整，跳过。状态: {fill_info.get('status')}")
            
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

                    if fill_info.get("status") == "filled" or (isinstance(fill_info.get("order"), dict) and fill_info["order"].get("status") == "filled"):
                        order_details = fill_info.get("order", {})

                        if 'avgPx' in order_details and float(order_details['avgPx']) > 0:
                            cover_price = float(order_details['avgPx'])
                        elif 'limitPx' in order_details:
                            cover_price = float(order_details['limitPx'])
                            logger.warning(f"平空单 {oid} 无法获取 avgPx，使用 limitPx {cover_price} 作为成交价。")
                        else:
                            logger.error(f"平空单 {oid} 无法确定成交价格，跳过此订单。 Fill info: {fill_info}")
                            continue

                        logger.info(f"🎯 检测到平空单成交: oid={oid}, 价格={cover_price}")

                        # 移除已成交平仓单
                        self.short_cover_orders = [o for o in self.short_cover_orders if o['oid'] != oid]
                        
                        # 重新挂一个做空单
                        short_price = self.round_to_tick_size(cover_price / (1 - self.tp))
                        logger.info(f"平空单成交，重新挂做空单: 价格={short_price}, 数量={self.eachgridamount}")
                        self.place_order_with_retry(self.COIN, False, self.eachgridamount, short_price, {"limit": {"tif": "Gtc"}}, cover_order_meta['index'])
                    else:
                        logger.info(f"订单 {oid} 状态不是 'filled' 或信息不完整，跳过。状态: {fill_info.get('status')}")

                except Exception as e:
                    logger.error(f"处理平空单 {oid} 成交时异常: {e}")
                    
    def place_order_with_retry(self, coin, is_buy, sz, px, order_type, grid_index=None, reduce_only=False):
        """带重试逻辑的下单函数，处理429限流"""
        max_retries = 5
        try:
            order_result = self.exchange.order(coin, is_buy, sz, px, order_type, reduce_only=reduce_only)
            if order_result.get("status") == "ok":
                statuses = order_result["response"]["data"].get("statuses", [])
                if statuses and "resting" in statuses[0]:
                    oid = statuses[0]["resting"]["oid"]
                    if is_buy and not reduce_only:
                        self.buy_orders.append({"index": grid_index, "oid": oid})
                    elif not is_buy: # This can be a short order or a TP order for long
                        self.sell_orders.append({"index": grid_index, "oid": oid, "is_tp": reduce_only})
                    logger.info(f"✅ 挂单成功 价格:{px} oid:{oid}")
                else:
                    logger.warning(f"挂单状态异常，将加入重试列表: {statuses}")
                    self.pending_orders_to_place.append({"coin": coin, "is_buy": is_buy, "sz": sz, "limit_px": px, "order_type": order_type, "original_index": grid_index, "reduce_only": reduce_only})
            else:
                 logger.error(f"❌ 挂单失败，将加入重试列表: {order_result}")
                 self.pending_orders_to_place.append({"coin": coin, "is_buy": is_buy, "sz": sz, "limit_px": px, "order_type": order_type, "original_index": grid_index, "reduce_only": reduce_only})
        except Exception as e:
            logger.error(f"❌ 挂单异常，将加入重试列表: {e}")
            self.pending_orders_to_place.append({"coin": coin, "is_buy": is_buy, "sz": sz, "limit_px": px, "order_type": order_type, "original_index": grid_index, "reduce_only": reduce_only})


    def _retry_pending_orders(self):
        if not self.pending_orders_to_place:
            return
        
        logger.info(f"🔄 重试 {len(self.pending_orders_to_place)} 个失败订单...")
        for order_info in self.pending_orders_to_place[:]:
            self.place_order_with_retry(order_info["coin"], order_info["is_buy"], order_info["sz"], order_info["limit_px"], order_info["order_type"], order_info["original_index"], order_info["reduce_only"])
            self.pending_orders_to_place.remove(order_info)

    def trader(self):
        self._retry_pending_orders()
        self.check_orders()

    def run(self):
        logger.info("🚀 网格交易策略启动")
        self.compute()
        
        while True:
            try:
                self.trader()
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