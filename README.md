# Hyperliquid 网格交易机器人

基于 Hyperliquid Python SDK 的自动化网格交易策略，支持多币种、参数灵活、实时行情、API限流保护、自动再平衡和风控校验。

## 主要功能
- 支持多币种网格交易（只做多或只做空）
- 网格参数、止盈止损、再平衡周期等均可配置
- 实时行情（WebSocket优先，REST兜底）
- API限流保护与自动重试
- 自动再平衡与风控校验
- 补充订单失败自动重试，杜绝幽灵仓位
- 详细日志与监控

## 快速开始
1. **克隆项目**
   ```bash
   git clone https://github.com/Web3Newcomer/HyperliquidGrid.git
   cd HyperliquidGrid
   ```
2. **安装依赖**
   ```bash
   pip install -r requirements.txt
   ```
3. **配置账户与策略**
   - `examples/config.json`：填写你的私钥和账户地址（注意不要上传到GitHub！）
   - `grid_config.json`：配置币种、网格参数、止盈止损等
4. **运行机器人**
   ```bash
   python3 Grid.py
   ```

## 配置说明
- `COIN`：交易币种，如 `BTC`、`ETH` 等
- `GRIDNUM`：网格数量
- `GRIDMAX/GRIDMIN`：网格区间（可自动）
- `TP`：每格止盈价差
- `EACHGRIDAMOUNT`：每格下单量
- `take_profit`/`stop_loss`：全局止盈止损百分比
- `enable_long_grid`/`enable_short_grid`：只做多或只做空（二选一）
- 详细参数见 `grid_config.json` 注释

## 常见问题
- **Q: 为什么不能双向网格？**
  A: 为防止仓位混乱和风险，已禁用双向网格模式，请只选择做多或做空。
- **Q: 如何避免泄露密钥？**
  A: 请勿上传 `config.json`，已在 `.gitignore` 自动屏蔽。
- **Q: 运行报错怎么办？**
  A: 检查依赖、配置文件格式、网络环境，或查看日志定位问题。

## 免责声明
本项目仅供学习与交流，量化交易有风险，使用前请充分测试并自担风险。

```bibtex
@misc{hyperliquid-python-sdk,
  author = {Hyperliquid},
  title = {SDK for Hyperliquid API trading with Python.},
  year = {2024},
  publisher = {GitHub},
  journal = {GitHub repository},
  howpublished = {\url{https://github.com/hyperliquid-dex/hyperliquid-python-sdk}}
}
```

## Credits

This project was generated with [`python-package-template`](https://github.com/TezRomacH/python-package-template).
