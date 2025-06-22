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
        if price_step is not None:
            self.price_step = price_step
        else:
            if self.COIN in ["BTC", "WBTC", "UBTC"]:
                self.price_step = 100
            elif self.COIN in ["ETH"]:
                self.price_step = 1
            else:
                self.price_step = 0.01
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
        self.stats['short_count'] = 0  # 做空成交次数
        self.stats['short_cover_count'] = 0  # 做空减仓次数
        self.stats['buy_volume'] = 0.0
        self.stats['sell_volume'] = 0.0
        self.stats['short_volume'] = 0.0  # 做空量
        self.stats['short_cover_volume'] = 0.0  # 做空减仓量
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
        # 启动 WebSocket 订阅 midprice
        def ws_callback(data):
            # 只处理 midprice 推送
            if isinstance(data, dict) and 'mid' in data:
                try:
                    self.ws_midprice = float(data['mid'])
                except Exception:
                    pass
            # 兼容部分推送格式
            elif isinstance(data, dict) and self.COIN in data:
                try:
                    self.ws_midprice = float(data[self.COIN])
                except Exception:
                    pass
        # 订阅 midprice
        try:
            # 订阅格式参考 hyperliquid.info.Info.subscribe
            sub = {"type": "mids", "coin": self.COIN}
            self.info.subscribe(sub, ws_callback)
            logger.info(f"WebSocket 已订阅 {self.COIN} midprice 实时行情")
        except Exception as e:
            logger.warning(f"WebSocket 订阅 midprice 失败: {e}")

    def get_midprice(self):
        # 优先用 WebSocket 推送的 midprice
        if self.ws_midprice is not None:
            return float(self.ws_midprice)
        # 兜底用 REST API
        try:
            all_mids = self.info.all_mids()
            midprice = all_mids.get(self.COIN, 0.0)
            return float(midprice) if midprice else 0.0
        except Exception as e:
            logger.warning(f"获取midprice异常: {e}")
            return 0.0

    def round_to_step(self, price):
        return round(price / self.price_step) * self.price_step

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

    def check_take_profit_stop_loss(self):
        if self.take_profit is None and self.stop_loss is None:
            return False
        entry = self.stats.get('entry_price', None)
        if entry is None:
            if self.stats['buy_volume'] > 0:
                self.stats['entry_price'] = self.stats['realized_entry'] / self.stats['buy_volume'] if self.stats['buy_volume'] > 0 else None
            else:
                return False
            entry = self.stats.get('entry_price', None)
        mid = self.get_midprice()
        pnl_pct = (mid - entry) / entry if entry else 0
        if self.take_profit and pnl_pct >= self.take_profit:
            logger.info(f"达到止盈线，当前收益率: {pnl_pct*100:.2f}%，自动平仓并停止策略")
            self.close_all_long()
            return True
        if self.stop_loss and pnl_pct <= -self.stop_loss:
            logger.info(f"达到止损线，当前收益率: {pnl_pct*100:.2f}% ，自动平仓并停止策略")
            self.close_all_long()
            return True
        return False

    def close_all_long(self):
        pos = self.get_position()
        if pos > 0:
            logger.info(f"平多 {pos} {self.COIN}")
            self.exchange.order(self.COIN, False, abs(pos), self.get_midprice(), {"limit": {"tif": "Gtc"}}, reduce_only=True)

    def compute(self):
        midprice = self.get_midprice()
        if not midprice or midprice <= 0:
            logger.error(f"无效的midprice: {midprice}, 无法计算网格")
            return
        if (self.gridmin is None or self.gridmax is None) and self.total_invest is not None:
            total_grid_amount = self.gridnum * self.eachgridamount * midprice
            if total_grid_amount > self.total_invest:
                max_eachgridamount = self.total_invest / (self.gridnum * midprice)
                # 限制精度，避免float_to_wire舍入错误
                max_eachgridamount = round(max_eachgridamount, 6)
                logger.warning(f"投资总金额不足，自动调整每格下单量为 {max_eachgridamount}")
                self.eachgridamount = max_eachgridamount
        
        # 检查最小下单量
        min_order_size = 0.0001  # BTC最小下单量
        if self.COIN in ["ETH"]:
            min_order_size = 0.001
        elif self.COIN in ["HYPE", "SOL", "MATIC"]:
            min_order_size = 0.1
        elif self.COIN in ["DOGE", "SHIB"]:
            min_order_size = 1.0
        
        if self.eachgridamount < min_order_size:
            logger.error(f"每格下单量 {self.eachgridamount} 小于最小下单量 {min_order_size}，请增加投资金额或减少网格数量")
            logger.error(f"建议：增加 total_invest 或减少 GRIDNUM，或手动设置更大的 EACHGRIDAMOUNT")
            return
        
        # 自动设置网格区间
        if self.gridmin is None or self.gridmax is None:
            price_step = midprice * 0.01
            self.gridmax = midprice + price_step * (self.gridnum // 2)
            self.gridmin = midprice - price_step * (self.gridnum // 2)
            logger.info(f"自动设置网格区间 gridmin={self.gridmin:.6f}, gridmax={self.gridmax:.6f}")
        
        if self.grid_ratio is not None and self.centered:
            logger.info(f"使用对称分布 grid_ratio={self.grid_ratio}, centered=True")
            n = self.gridnum
            prices = []
            if n % 2 == 1:
                half = n // 2
                for i in range(half, 0, -1):
                    prices.append(self.round_to_step(midprice * (1 - self.grid_ratio) ** i))
                prices.append(self.round_to_step(midprice))
                for i in range(1, half + 1):
                    prices.append(self.round_to_step(midprice * (1 + self.grid_ratio) ** i))
            else:
                half = n // 2
                for i in range(half, 0, -1):
                    prices.append(self.round_to_step(midprice * (1 - self.grid_ratio) ** (i - 0.5)))
                for i in range(1, half + 1):
                    prices.append(self.round_to_step(midprice * (1 + self.grid_ratio) ** (i - 0.5)))
            self.eachprice = sorted(prices)
        elif self.grid_ratio is not None:
            logger.info(f"使用自定义比例 grid_ratio={self.grid_ratio}")
            gridmin = self.gridmin if self.gridmin is not None else round(midprice * 0.98, 2)
            self.eachprice = [self.round_to_step(gridmin * (1 + self.grid_ratio) ** i) for i in range(self.gridnum)]
        else:
            logger.info(f"自动适配价格步长 price_step={self.price_step}")
            pricestep = (self.gridmax - self.gridmin) / self.gridnum
            self.eachprice = [self.round_to_step(self.gridmin + i * pricestep) for i in range(self.gridnum)]
        logger.info(f"Grid levels: {self.eachprice}")
        logger.info(f"Midprice: {midprice}")
        
        # 第一单以现价成交
        if self.enable_long_grid:
            try:
                #【修复】为了保证IOC订单立即成交，主动增加一个价格步长的滑点
                slippage_price = self.round_to_step(midprice) + self.price_step
                logger.info(f"第一单以现价 {midprice} (滑点后: {slippage_price}) 成交 {self.eachgridamount} {self.COIN}...")
                logger.info(f"第一单参数: COIN={self.COIN}, 数量={self.eachgridamount}, 价格={slippage_price}, TIF=Ioc")
                
                order_result = self.exchange.order(self.COIN, True, self.eachgridamount, slippage_price, {"limit": {"tif": "Ioc"}})
                logger.info(f"第一单API响应: {order_result}")
                
                if order_result["status"] == "ok":
                    statuses = order_result["response"]["data"]["statuses"]
                    logger.info(f"第一单状态: {statuses[0]}")
                    
                    if "filled" in statuses[0]:
                        #【修复】适配API变更：px -> avgPx, sz -> totalSz
                        filled_price = statuses[0]["filled"]["avgPx"]
                        filled_sz = statuses[0]["filled"]["totalSz"]
                        logger.info(f"✅ 第一单成交成功: 价格={filled_price}, 数量={filled_sz}")
                        # 立即挂对应的卖单
                        sell_price = self.round_to_step(float(filled_price) * (1 + self.tp))
                        
                        # 【重要风控】防止止盈价差过小导致在相同价位开平仓
                        if sell_price <= float(filled_price):
                            original_sell_price = sell_price
                            sell_price = self.round_to_step(float(filled_price) + self.price_step)
                            logger.error(f"【严重警告】TP值({self.tp})过小，导致计算出的卖价({original_sell_price}) <= 买价({filled_price})。")
                            logger.error(f"为防止亏损，已强制将卖价调整为 {sell_price} (买价 + 一个价格步长)。请调大您的TP值！")

                        logger.info(f"挂对应卖单: 价格={sell_price}, 数量={filled_sz}")
                        
                        #【修复】确保下单数量为float类型
                        sell_order_result = self.exchange.order(self.COIN, False, float(filled_sz), sell_price, {"limit": {"tif": "Gtc"}}, reduce_only=True)
                        logger.info(f"对应卖单API响应: {sell_order_result}")
                        
                        if sell_order_result["status"] == "ok":
                            sell_statuses = sell_order_result["response"]["data"]["statuses"]
                            if "resting" in sell_statuses[0]:
                                sell_oid = sell_statuses[0]["resting"]["oid"]
                                logger.info(f"✅ 对应卖单已挂出: 价格={sell_price}, oid={sell_oid}")
                                self.sell_orders.append({"index": 0, "oid": sell_oid, "activated": True})
                            else:
                                logger.warning(f"对应卖单状态异常: {sell_statuses[0]}")
                        else:
                            logger.error(f"❌ 对应卖单挂出失败: {sell_order_result}")
                    else:
                        logger.warning(f"第一单未成交: {statuses[0]}")
                else:
                    logger.error(f"❌ 第一单下单失败: {order_result}")
            except Exception as e:
                logger.error(f"❌ 第一单执行异常: {e}")
                import traceback
                logger.error(f"异常堆栈: {traceback.format_exc()}")
        
        if self.enable_short_grid:
            try:
                #【修复】为了保证IOC订单立即成交，主动增加一个价格步长的滑点
                slippage_price = self.round_to_step(midprice) - self.price_step
                logger.info(f"第一单以现价 {midprice} (滑点后: {slippage_price}) 做空 {self.eachgridamount} {self.COIN}...")
                logger.info(f"第一单做空参数: COIN={self.COIN}, 数量={self.eachgridamount}, 价格={slippage_price}, TIF=Ioc")
                
                order_result = self.exchange.order(self.COIN, False, self.eachgridamount, slippage_price, {"limit": {"tif": "Ioc"}}, reduce_only=False)
                logger.info(f"第一单做空API响应: {order_result}")
                
                if order_result["status"] == "ok":
                    statuses = order_result["response"]["data"]["statuses"]
                    logger.info(f"第一单做空状态: {statuses[0]}")
                    
                    if "filled" in statuses[0]:
                        #【修复】适配API变更：px -> avgPx, sz -> totalSz
                        filled_price = statuses[0]["filled"]["avgPx"]
                        filled_sz = statuses[0]["filled"]["totalSz"]
                        logger.info(f"✅ 第一单做空成交成功: 价格={filled_price}, 数量={filled_sz}")
                        # 立即挂对应的买单
                        cover_price = self.round_to_step(float(filled_price) * (1 - self.tp))
                        
                        # 【重要风控】防止止盈价差过小导致在相同价位开平仓
                        if cover_price >= float(filled_price):
                            original_cover_price = cover_price
                            cover_price = self.round_to_step(float(filled_price) - self.price_step)
                            logger.error(f"【严重警告】TP值({self.tp})过小，导致计算出的平仓买价({original_cover_price}) >= 开仓卖价({filled_price})。")
                            logger.error(f"为防止亏损，已强制将平仓买价调整为 {cover_price} (卖价 - 一个价格步长)。请调大您的TP值！")

                        logger.info(f"挂对应买单: 价格={cover_price}, 数量={filled_sz}")
                        
                        #【修复】确保下单数量为float类型
                        cover_order_result = self.exchange.order(self.COIN, True, float(filled_sz), cover_price, {"limit": {"tif": "Gtc"}}, reduce_only=True)
                        logger.info(f"对应买单API响应: {cover_order_result}")
                        
                        if cover_order_result["status"] == "ok":
                            cover_statuses = cover_order_result["response"]["data"]["statuses"]
                            if "resting" in cover_statuses[0]:
                                cover_oid = cover_statuses[0]["resting"]["oid"]
                                logger.info(f"✅ 对应买单已挂出: 价格={cover_price}, oid={cover_oid}")
                                self.short_cover_orders.append({"index": 0, "oid": cover_oid, "activated": True})
                            else:
                                logger.warning(f"对应买单状态异常: {cover_statuses[0]}")
                        else:
                            logger.error(f"❌ 对应买单挂出失败: {cover_order_result}")
                    else:
                        logger.warning(f"第一单做空未成交: {statuses[0]}")
                else:
                    logger.error(f"❌ 第一单做空下单失败: {order_result}")
            except Exception as e:
                logger.error(f"❌ 第一单做空执行异常: {e}")
                import traceback
                logger.error(f"异常堆栈: {traceback.format_exc()}")
        
        # 初始挂网格单
        if self.enable_long_grid:
            logger.info("开始挂做多网格买单...")
            for i, price in enumerate(self.eachprice):
                if price > midprice:
                    continue  # 只挂低于等于中间价的买单
                order_result = self.exchange.order(self.COIN, True, self.eachgridamount, price, {"limit": {"tif": "Gtc"}})
                if order_result["status"] == "ok":
                    statuses = order_result["response"]["data"]["statuses"]
                    if "resting" in statuses[0]:
                        oid = statuses[0]["resting"]["oid"]
                        logger.info(f"✅ Buy order placed at {price}, oid: {oid}")
                        self.buy_orders.append({"index": i, "oid": oid, "activated": True})
                    elif "filled" in statuses[0]:
                        logger.info(f"Buy order at {price} filled immediately.")
                        self._handle_filled_buy_order(i, price)
                    else:
                        logger.warning(f"Unknown order status: {statuses[0]}")
                else:
                    logger.error(f"❌ Buy order failed: {order_result}")

        if self.enable_short_grid:
            logger.info("开始挂做空网格卖单...")
            for i, price in enumerate(self.eachprice):
                if price < midprice:
                    continue # 只挂高于等于中间价的卖单
                order_result = self.exchange.order(self.COIN, False, self.eachgridamount, price, {"limit": {"tif": "Gtc"}}, reduce_only=False)
                if order_result["status"] == "ok":
                    statuses = order_result["response"]["data"]["statuses"]
                    if "resting" in statuses[0]:
                        oid = statuses[0]["resting"]["oid"]
                        logger.info(f"✅ Short order placed at {price}, oid: {oid}")
                        self.short_orders.append({"index": i, "oid": oid, "activated": True})
                    elif "filled" in statuses[0]:
                        logger.info(f"Short order at {price} filled immediately.")
                        self._handle_filled_short_order(i, price)
                    else:
                        logger.warning(f"Unknown order status: {statuses[0]}")
                else:
                    logger.error(f"❌ Short order failed: {order_result}")

    def _handle_filled_buy_order(self, order_index, buy_price):
        logger.info(f"处理已成交买单, 网格索引: {order_index}, 价格: {buy_price}")
        self.stats['buy_count'] += 1
        self.stats['buy_volume'] += self.eachgridamount
        self.stats['realized_entry'] = self.stats.get('realized_entry', 0.0) + buy_price * self.eachgridamount
        
        # 【修复】移除下单前的持仓检查，以解决因API状态延迟导致无法挂出卖单的问题。
        # 我们相信 query_order_by_oid 的 'filled' 状态，并直接尝试挂出对应的 reduce_only 卖单。
        # 如果因状态延迟等原因实际没有持仓，交易所会拒绝这个 reduce_only 订单，这是安全的。
        
        sell_price = self.round_to_step(buy_price * (1 + self.tp))
        
        # 【重要风控】防止止盈价差过小导致在相同价位开平仓
        if sell_price <= buy_price:
            original_sell_price = sell_price
            sell_price = self.round_to_step(buy_price + self.price_step)
            logger.error(f"【严重警告】TP值({self.tp})过小，导致计算出的卖价({original_sell_price}) <= 买价({buy_price})。")
            logger.error(f"为防止亏损，已强制将卖价调整为 {sell_price} (买价 + 一个价格步长)。请调大您的TP值！")

        logger.info(f"为成交的买单挂出对应卖单: 价格={sell_price}, 数量={self.eachgridamount}")
        order_result = self.exchange.order(self.COIN, False, self.eachgridamount, sell_price, {"limit": {"tif": "Gtc"}}, reduce_only=True)
        if order_result.get("status") == "ok":
            statuses = order_result["response"]["data"].get("statuses", [])
            if statuses and "resting" in statuses[0]:
                oid = statuses[0]["resting"]["oid"]
                logger.info(f"✅ Sell order placed at {sell_price}, oid: {oid}")
                self.sell_orders.append({"index": order_index, "oid": oid, "activated": True})
            else:
                logger.warning(f"Sell order placement did not result in a resting order: {statuses}")
                # 即使没有resting，也可能被立即filled或遇到其他情况，统一加入待重试列表确保鲁棒性
                self.pending_orders_to_place.append({
                    "original_index": order_index,
                    "coin": self.COIN, "is_buy": False, "sz": self.eachgridamount,
                    "limit_px": sell_price, "order_type": {"limit": {"tif": "Gtc"}}, "reduce_only": True
                })
        else:
            logger.error(f"❌ Sell order补充失败: {order_result}")
            self.pending_orders_to_place.append({
                "original_index": order_index,
                "coin": self.COIN, "is_buy": False, "sz": self.eachgridamount,
                "limit_px": sell_price, "order_type": {"limit": {"tif": "Gtc"}}, "reduce_only": True
            })

    def _handle_filled_short_order(self, order_index, short_price):
        logger.info(f"处理已成交做空单, 网格索引: {order_index}, 价格: {short_price}")
        self.stats['short_count'] += 1
        self.stats['short_volume'] += self.eachgridamount
        
        # 【修复】移除下单前的持仓检查
        cover_price = self.round_to_step(short_price * (1 - self.tp))

        # 【重要风控】防止止盈价差过小导致在相同价位开平仓
        if cover_price >= short_price:
            original_cover_price = cover_price
            cover_price = self.round_to_step(short_price - self.price_step)
            logger.error(f"【严重警告】TP值({self.tp})过小，导致计算出的平仓买价({original_cover_price}) >= 开仓卖价({short_price})。")
            logger.error(f"为防止亏损，已强制将平仓买价调整为 {cover_price} (卖价 - 一个价格步长)。请调大您的TP值！")

        logger.info(f"为成交的做空单挂出对应买单: 价格={cover_price}, 数量={self.eachgridamount}")
        order_result = self.exchange.order(self.COIN, True, self.eachgridamount, cover_price, {"limit": {"tif": "Gtc"}}, reduce_only=True)
        if order_result.get("status") == "ok":
            statuses = order_result["response"]["data"].get("statuses", [])
            if statuses and "resting" in statuses[0]:
                oid = statuses[0]["resting"]["oid"]
                logger.info(f"✅ Short cover order placed at {cover_price}, oid: {oid}")
                self.short_cover_orders.append({"index": order_index, "oid": oid, "activated": True})
            else:
                logger.warning(f"Short cover placement did not result in a resting order: {statuses}")
                self.pending_orders_to_place.append({
                    "original_index": order_index,
                    "coin": self.COIN, "is_buy": True, "sz": self.eachgridamount,
                    "limit_px": cover_price, "order_type": {"limit": {"tif": "Gtc"}}, "reduce_only": True
                })
        else:
            logger.error(f"❌ Short cover order补充失败: {order_result}")
            self.pending_orders_to_place.append({
                "original_index": order_index,
                "coin": self.COIN, "is_buy": True, "sz": self.eachgridamount,
                "limit_px": cover_price, "order_type": {"limit": {"tif": "Gtc"}}, "reduce_only": True
            })

    def check_orders(self):
        if self.check_take_profit_stop_loss():
            logger.info("已触发止盈/止损，停止策略。"); exit(0)
        
        if self.enable_long_grid:
            for buy_order in self.buy_orders[:]:
                if buy_order["activated"] and buy_order["oid"] not in self.filled_buy_oids:
                    try:
                        order_status = self.info.query_order_by_oid(self.address, buy_order["oid"])
                        if order_status.get("order", {}).get("status") == "filled":
                            self.filled_buy_oids.add(buy_order["oid"])
                            buy_price = self.eachprice[buy_order["index"]]
                            self._handle_filled_buy_order(buy_order["index"], buy_price)
                            self.buy_orders.remove(buy_order)
                    except Exception as e:
                        logger.warning(f"查询买单状态异常，跳过本次检查: {e}")
                        continue
            
            for sell_order in self.sell_orders[:]:
                if sell_order["activated"] and sell_order["oid"] not in self.filled_sell_oids:
                    try:
                        order_status = self.info.query_order_by_oid(self.address, sell_order["oid"])
                        if order_status.get("order", {}).get("status") == "filled":
                            self.filled_sell_oids.add(sell_order["oid"])
                            self.stats['sell_count'] += 1
                            self.stats['sell_volume'] += self.eachgridamount
                            buy_price = self.eachprice[sell_order["index"]]
                            order_result = self.exchange.order(self.COIN, True, self.eachgridamount, buy_price, {"limit": {"tif": "Gtc"}})
                            if order_result.get("status") == "ok":
                                statuses = order_result["response"]["data"].get("statuses", [])
                                oid = statuses[0].get("resting", {}).get("oid", 0)
                                logger.info(f"✅ Buy order placed at {buy_price}, oid: {oid}")
                                self.buy_orders.append({"index": sell_order["index"], "oid": oid, "activated": True})
                                self.sell_orders.remove(sell_order)
                            else:
                                logger.error(f"❌ Buy order补充失败: {order_result}")
                                self.pending_orders_to_place.append({
                                    "original_index": sell_order["index"],
                                    "coin": self.COIN, "is_buy": True, "sz": self.eachgridamount,
                                    "limit_px": buy_price, "order_type": {"limit": {"tif": "Gtc"}}, "reduce_only": False
                                })
                    except Exception as e:
                        logger.warning(f"查询卖单状态异常，跳过本次检查: {e}")
                        continue

        if self.enable_short_grid:
            for short_order in self.short_orders[:]:
                if short_order["activated"] and short_order["oid"] not in self.filled_short_oids:
                    try:
                        order_status = self.info.query_order_by_oid(self.address, short_order["oid"])
                        if order_status.get("order", {}).get("status") == "filled":
                            self.filled_short_oids.add(short_order["oid"])
                            short_price = self.eachprice[short_order["index"]]
                            self._handle_filled_short_order(short_order["index"], short_price)
                            self.short_orders.remove(short_order)
                    except Exception as e:
                        logger.warning(f"查询做空单状态异常，跳过本次检查: {e}")
                        continue
            
            for cover_order in self.short_cover_orders[:]:
                if cover_order["activated"] and cover_order["oid"] not in self.filled_short_cover_oids:
                    try:
                        order_status = self.info.query_order_by_oid(self.address, cover_order["oid"])
                        if order_status.get("order", {}).get("status") == "filled":
                            self.filled_short_cover_oids.add(cover_order["oid"])
                            self.stats['short_cover_count'] += 1
                            self.stats['short_cover_volume'] += self.eachgridamount
                            short_price = self.eachprice[cover_order["index"]]
                            order_result = self.exchange.order(self.COIN, False, self.eachgridamount, short_price, {"limit": {"tif": "Gtc"}})
                            if order_result.get("status") == "ok":
                                statuses = order_result["response"]["data"].get("statuses", [])
                                oid = statuses[0].get("resting", {}).get("oid", 0)
                                logger.info(f"✅ Short order placed at {short_price}, oid: {oid}")
                                self.short_orders.append({"index": cover_order["index"], "oid": oid, "activated": True})
                                self.short_cover_orders.remove(cover_order)
                            else:
                                logger.error(f"❌ Short order补充失败: {order_result}")
                                self.pending_orders_to_place.append({
                                    "original_index": cover_order["index"],
                                    "coin": self.COIN, "is_buy": False, "sz": self.eachgridamount,
                                    "limit_px": short_price, "order_type": {"limit": {"tif": "Gtc"}}, "reduce_only": False
                                })
                    except Exception as e:
                        logger.warning(f"查询做空减仓单状态异常，跳过本次检查: {e}")
                        continue

    def print_stats(self):
        logger.info("\n===== 交易统计 =====")
        if self.enable_long_grid:
            logger.info(f"累计买单成交次数: {int(self.stats['buy_count'])}")
            logger.info(f"累计卖单成交次数: {int(self.stats['sell_count'])}")
            logger.info(f"累计买入量: {self.stats['buy_volume']}")
            logger.info(f"累计卖出量: {self.stats['sell_volume']}")
        if self.enable_short_grid:
            logger.info(f"累计做空成交次数: {int(self.stats['short_count'])}")
            logger.info(f"累计做空减仓次数: {int(self.stats['short_cover_count'])}")
            logger.info(f"累计做空量: {self.stats['short_volume']}")
            logger.info(f"累计做空减仓量: {self.stats['short_cover_volume']}")
        logger.info(f"已实现盈利: {self.stats['realized_pnl']:.6f}")
        # 未实现盈亏估算
        midprice = self.get_midprice()
        holding = self.stats['buy_volume'] - self.stats['sell_volume']
        self.stats['unrealized_pnl'] = holding * (midprice - self.eachprice[0]) if holding > 0 else 0.0
        logger.info(f"未实现盈亏: {self.stats['unrealized_pnl']:.6f}")
        logger.info(f"当前持仓: {holding}")
        logger.info(f"最新 midprice (WebSocket): {midprice}")
        logger.info("====================\n")

    def _retry_pending_orders(self):
        # 检查并重试下单失败的补充订单
        for pending_order in self.pending_orders_to_place[:]:
            logger.info(f"尝试重下失败的补充订单: {pending_order}")
            order_result = self.exchange.order(
                pending_order['coin'],
                pending_order['is_buy'],
                pending_order['sz'],
                pending_order['limit_px'],
                pending_order['order_type'],
                reduce_only=pending_order['reduce_only']
            )
            if order_result.get("status") == "ok":
                statuses = order_result["response"]["data"].get("statuses", [])
                if statuses and "resting" in statuses[0]:
                    oid = statuses[0]["resting"]["oid"]
                    logger.info(f"✅ 补充订单重下成功, oid: {oid}")
                    # 根据订单类型，将其添加到正确的本地列表中
                    if pending_order['is_buy'] and pending_order['reduce_only']: # 做空平仓单
                         self.short_cover_orders.append({"index": pending_order["original_index"], "oid": oid, "activated": True})
                    elif not pending_order['is_buy'] and not pending_order['reduce_only']: # 做空开仓单
                        self.short_orders.append({"index": pending_order["original_index"], "oid": oid, "activated": True})
                    elif not pending_order['is_buy'] and pending_order['reduce_only']: # 做多平仓单
                        self.sell_orders.append({"index": pending_order["original_index"], "oid": oid, "activated": True})
                    elif pending_order['is_buy'] and not pending_order['reduce_only']: # 做多开仓单
                        self.buy_orders.append({"index": pending_order["original_index"], "oid": oid, "activated": True})
                    
                    self.pending_orders_to_place.remove(pending_order) # 从待办列表中移除
                else:
                    logger.warning(f"补充订单重下后状态异常: {statuses}")
            else:
                logger.error(f"❌ 补充订单重下失败: {order_result}")

    def trader(self):
        self._retry_pending_orders() # 优先处理失败的补充订单
        self.check_orders()
        # 每分钟输出一次统计
        now = time.time()
        if now - self.stats['last_log_time'] >= 60:
            self.print_stats()
            self.stats['last_log_time'] = now 

    def pre_rebalance_risk_check(self):
        """
        再平衡前风控：如持仓过大、余额不足、API异常等
        返回True表示通过，False表示跳过本次再平衡
        """
        pos = self.get_position()
        max_pos = self.eachgridamount * self.gridnum * 2
        if abs(pos) > max_pos:
            logger.warning(f"[风控] 持仓过大: {pos}, 超过最大允许: {max_pos}")
            return False
        # 可扩展更多风控条件...
        return True

    def post_rebalance_risk_check(self):
        """
        再平衡后风控：如挂单数量异常、API返回异常等
        返回True表示通过，False表示报警
        """
        try:
            open_orders = self.info.open_orders(self.address)
            if len(open_orders) < self.gridnum:
                logger.warning(f"[风控] 挂单数量异常: {len(open_orders)} < 预期{self.gridnum}")
                return False
        except Exception as e:
            logger.warning(f"[风控] 查询挂单异常: {e}")
            return False
        # 可扩展更多风控条件...
        return True

    def rebalance(self):
        """
        每小时再平衡：撤销所有未成交买卖单，重新计算网格并挂单
        """
        if not self.pre_rebalance_risk_check():
            logger.warning("[再平衡] 风控不通过，跳过本次再平衡")
            return

        logger.info("[再平衡] 开始撤销所有未成交买卖单...")
        try:
            open_orders = self.info.open_orders(self.address)
        except Exception as e:
            logger.error(f"[再平衡] 无法获取当前挂单，跳过本次再平衡: {e}")
            return
        
        # 筛选出本策略相关的挂单
        our_oids = set()
        if self.enable_long_grid:
            our_oids.update([order['oid'] for order in self.buy_orders + self.sell_orders if order['activated']])
        if self.enable_short_grid:
            our_oids.update([order['oid'] for order in self.short_orders + self.short_cover_orders if order['activated']])
        
        # 找出实际在交易所挂单列表中的、属于本策略的订单
        cancel_oids = [order['oid'] for order in open_orders if order['oid'] in our_oids]

        if not cancel_oids:
            logger.info("[再平衡] 检测到无需撤销的挂单。")
        else:
            cancel_requests = [{"coin": self.COIN, "oid": oid} for oid in cancel_oids]
            try:
                self.exchange.bulk_cancel(cancel_requests)
                logger.info(f"[再平衡] 已发送 {len(cancel_oids)} 个撤单请求，开始确认状态...")

                # 确认撤单成功
                retries = 5
                for i in range(retries):
                    time.sleep(2) # 等待交易所处理
                    remaining_open_orders = self.info.open_orders(self.address)
                    remaining_oids = {order['oid'] for order in remaining_open_orders}
                    
                    still_open = [oid for oid in cancel_oids if oid in remaining_oids]
                    if not still_open:
                        logger.info("[再平衡] 所有目标挂单已成功撤销。")
                        break
                    else:
                        logger.warning(f"[再平衡] 仍有 {len(still_open)} 个订单待撤销，继续检查... (尝试 {i+1}/{retries})")
                else:
                    logger.error("[再平衡] 撤单确认超时，部分订单可能仍挂在交易所，为安全起见，跳过本次再平衡！")
                    return # 关键：撤单不成功，则不继续执行
                    
            except Exception as e:
                logger.error(f"[再平衡] 批量撤单请求异常，跳过本次再平衡: {e}")
                return

        # 安全地清空本地状态并重新计算网格
        logger.info("[再平衡] 安全清空本地状态并重新计算网格...")
        if self.enable_long_grid:
            self.buy_orders.clear()
            self.sell_orders.clear()
            self.filled_buy_oids.clear()
            self.filled_sell_oids.clear()
        if self.enable_short_grid:
            self.short_orders.clear()
            self.short_cover_orders.clear()
            self.filled_short_oids.clear()
            self.filled_short_cover_oids.clear()
        self.pending_orders_to_place.clear()

        self.compute()

        if not self.post_rebalance_risk_check():
            logger.warning("[再平衡] 再平衡后风控异常，请人工检查！") 