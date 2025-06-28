import json
import time
import os
import logging
from datetime import datetime
from hyperliquid.grid_trading import GridTrading, setup
from hyperliquid.utils import constants
import requests

# ========== 配置文件路径 ==========
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "examples", "config.json")
GRID_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "grid_config.json")
# ==================================

# 创建logs目录
logs_dir = os.path.join(os.path.dirname(__file__), "logs")
if not os.path.exists(logs_dir):
    os.makedirs(logs_dir)

# 生成日志文件名（包含时间戳）
log_filename = f"grid_trading_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
log_filepath = os.path.join(logs_dir, log_filename)

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler(log_filepath, encoding='utf-8'),  # 文件日志
        logging.StreamHandler()  # 控制台日志
    ]
)
logger = logging.getLogger(__name__)

# 记录启动信息
logger.info("=" * 50)
logger.info("网格交易机器人启动")
logger.info(f"日志文件: {log_filepath}")
logger.info("=" * 50)

def load_account_config():
    with open(CONFIG_PATH, "r") as f:
        config = json.load(f)
    private_key = config["secret_key"]
    address = config["account_address"]
    return private_key, address

def load_grid_config():
    default = {
        "COIN": "HYPE",
        "GRIDNUM": 10,
        "GRIDMAX": None,
        "GRIDMIN": None,
        "TP": 0.2,
        "EACHGRIDAMOUNT": 0.6,
        "HASS_SPOT": True,
        "total_invest": None,
        "price_step": None,
        "grid_ratio": None,
        "centered": False
    }
    if os.path.exists(GRID_CONFIG_PATH):
        with open(GRID_CONFIG_PATH, "r") as f:
            user_cfg = json.load(f)
        default.update(user_cfg)
    return default

def log_grid_status(trading):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        midprice = float(trading.get_midprice())
    except Exception as e:
        midprice = None
    logger.info(f"[网格监控] 当前时间: {now}")
    logger.info(f"[网格监控] 当前midprice: {midprice}")
    logger.info(f"[网格监控] 网格价格区间: {trading.eachprice}")
    
    if trading.enable_long_grid:
        logger.info(f"[网格监控] 做多买单状态:")
        for order in trading.buy_orders:
            logger.info(f"  网格{order['index']} 价格: {trading.eachprice[order['index']]} oid: {order['oid']}")
        logger.info(f"[网格监控] 做多卖单状态:")
        for order in trading.sell_orders:
            # 计算卖单的止盈价格
            tp_price = trading.round_to_tick_size(trading.eachprice[order['index']] * (1 + trading.tp))
            logger.info(f"  网格{order['index']} 价格: {tp_price} oid: {order['oid']}")

    if trading.enable_short_grid:
        logger.info(f"[网格监控] 做空卖单状态:")
        for order in trading.short_orders:
            logger.info(f"  网格{order['index']} 价格: {trading.eachprice[order['index']]} oid: {order['oid']}")
        logger.info(f"[网格监控] 做空买单状态:")
        for order in trading.short_cover_orders:
            # 计算平空单的止盈价格
            tp_price = trading.round_to_tick_size(trading.eachprice[order['index']] * (1 - trading.tp))
            logger.info(f"  网格{order['index']} 价格: {tp_price} oid: {order['oid']}")

def main():
    private_key, address = load_account_config()
    grid_cfg = load_grid_config()
    # 自动重试机制
    for i in range(3):
        try:
            address, info, exchange = setup(
                base_url=constants.MAINNET_API_URL,
                skip_ws=False,
                private_key=private_key,
                address=address
            )
            break
        except requests.exceptions.ConnectionError as e:
            logger.warning(f'网络连接失败，正在重试({i+1}/3)...')
            time.sleep(3)
    else:
        logger.error('多次重试后依然无法连接，请检查网络环境或VPN！')
        exit(1)

    # 取消所有现存订单，确保从一个干净的状态开始
    try:
        logger.info(f"正在检查并取消币种 {grid_cfg['COIN']} 的所有现存挂单...")
        open_orders = info.open_orders(address)
        orders_to_cancel = []
        for o in open_orders:
            try:
                if o.get("coin") == grid_cfg["COIN"] and o.get("oid") is not None:
                    orders_to_cancel.append({"coin": o["coin"], "oid": o["oid"]})
            except Exception as e:
                logger.warning(f"跳过异常订单对象: {o}, 错误: {e}")
        if orders_to_cancel:
            logger.warning(f"发现 {len(orders_to_cancel)} 个现存挂单，正在取消...")
            try:
                exchange.bulk_cancel(orders_to_cancel)
                time.sleep(1) # 等待交易所处理
                logger.info("所有现存挂单已成功取消。")
            except Exception as e:
                logger.error(f"批量取消挂单时发生错误: {e}")
        else:
            logger.info("没有发现现存挂单，干净启动。")
    except Exception as e:
        logger.error(f"取消现存挂单时发生错误: {e}。请手动检查交易所。")
        # exit(1)

    all_mids = info.all_mids()
    logger.info("可用币种如下：")
    logger.info(list(all_mids.keys()))
    if grid_cfg["COIN"] not in all_mids:
        logger.error(f"错误：你配置的 COIN='{grid_cfg['COIN']}' 不在可用币种中，请修改 grid_config.json 里的 COIN 参数！")
        return
    user_state = info.user_state(address)
    positions = user_state.get("assetPositions", [])
    if positions:
        logger.info("Open positions:")
        for position in positions:
            logger.info(json.dumps(position["position"], indent=2))
    else:
        logger.info("No open positions.")
    trading = GridTrading(
        address, info, exchange,
        grid_cfg["COIN"], grid_cfg["GRIDNUM"], grid_cfg["GRIDMAX"], grid_cfg["GRIDMIN"],
        grid_cfg["TP"], grid_cfg["EACHGRIDAMOUNT"], grid_cfg["HASS_SPOT"],
        grid_cfg.get("total_invest"), grid_cfg.get("price_step"), grid_cfg.get("grid_ratio"),
        grid_cfg.get("centered", False), grid_cfg.get("take_profit"), grid_cfg.get("stop_loss"),
        grid_cfg.get("enable_long_grid", True), grid_cfg.get("enable_short_grid", False)
    )
    trading.compute()
    last_log_time = time.time()
    while True:
        trading.trader()
        now = time.time()
        if now - last_log_time >= 60:
            log_grid_status(trading)
            last_log_time = now
        time.sleep(2)

if __name__ == "__main__":
    main()