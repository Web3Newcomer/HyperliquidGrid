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

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

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
        "centered": False,
        "rebalance_interval": 1800
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
            status = '激活' if order['activated'] else '未激活'
            logger.info(f"  网格{order['index']} 价格: {trading.eachprice[order['index']]} oid: {order['oid']} 状态: {status}")
        logger.info(f"[网格监控] 做多卖单状态:")
        for order in trading.sell_orders:
            status = '激活' if order['activated'] else '未激活'
            logger.info(f"  网格{order['index']} 价格: {trading.round_to_step(trading.eachprice[order['index']] + trading.tp)} oid: {order['oid']} 状态: {status}")

    if trading.enable_short_grid:
        logger.info(f"[网格监控] 做空卖单状态:")
        for order in trading.short_orders:
            status = '激活' if order['activated'] else '未激活'
            logger.info(f"  网格{order['index']} 价格: {trading.eachprice[order['index']]} oid: {order['oid']} 状态: {status}")
        logger.info(f"[网格监控] 做空买单状态:")
        for order in trading.short_cover_orders:
            status = '激活' if order['activated'] else '未激活'
            logger.info(f"  网格{order['index']} 价格: {trading.round_to_step(trading.eachprice[order['index']] - trading.tp)} oid: {order['oid']} 状态: {status}")

def main():
    private_key, address = load_account_config()
    grid_cfg = load_grid_config()
    rebalance_interval = grid_cfg.get("rebalance_interval", 3600)
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
            print(f'网络连接失败，正在重试({i+1}/3)...')
            time.sleep(3)
    else:
        print('多次重试后依然无法连接，请检查网络环境或VPN！')
        exit(1)
    all_mids = info.all_mids()
    print("可用币种如下：")
    print(list(all_mids.keys()))
    if grid_cfg["COIN"] not in all_mids:
        print(f"错误：你配置的 COIN='{grid_cfg['COIN']}' 不在可用币种中，请修改 grid_config.json 里的 COIN 参数！")
        return
    user_state = info.user_state(address)
    positions = user_state.get("assetPositions", [])
    if positions:
        print("Open positions:")
        for position in positions:
            print(json.dumps(position["position"], indent=2))
    else:
        print("No open positions.")
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
    rebalance_time = time.time()
    while True:
        trading.trader()
        now = time.time()
        if now - last_log_time >= 60:
            log_grid_status(trading)
            last_log_time = now
        # 每rebalance_interval秒再平衡
        if now - rebalance_time >= rebalance_interval:
            logger.info(f"[定时] 触发再平衡，周期{rebalance_interval}s ...")
            trading.rebalance()
            rebalance_time = now
        time.sleep(2)

if __name__ == "__main__":
    main()