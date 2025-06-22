# 网格交易配置文件说明

## 配置文件参数详解

### 基础参数
- **COIN**: 交易币种，如 BTC、ETH、HYPE 等
- **GRIDNUM**: 网格数量，建议6-20个
- **GRIDMAX**: 网格最高价格，null表示自动计算
- **GRIDMIN**: 网格最低价格，null表示自动计算
- **TP**: 每格利润，如0.003表示0.3%
- **EACHGRIDAMOUNT**: 每格交易数量

### 交易类型
- **HASS_SPOT**: 是否现货交易，true为现货，false为合约

### 自动计算参数
- **total_invest**: 总投资金额，null表示不限制
- **price_step**: 价格步长，null表示自动适配
- **grid_ratio**: 网格比例，null表示等间距
- **centered**: 是否以现价为中心对称分布

### 风控参数
- **take_profit**: 止盈比例，如0.05表示5%
- **stop_loss**: 止损比例，如0.03表示3%

### 系统参数
- **rebalance_interval**: 再平衡周期，单位秒，3600表示1小时

### 网格模式控制
- **enable_long_grid**: 是否启用做多网格
- **enable_short_grid**: 是否启用做空网格

## 网格模式配置

### 1. 只做多网格
```json
{
  "enable_long_grid": true,
  "enable_short_grid": false
}
```
- 在现价和低于现价的网格挂买单
- 买单成交后补充卖单减仓
- 卖单成交后补充买单加仓

### 2. 只做空网格
```json
{
  "enable_long_grid": false,
  "enable_short_grid": true
}
```
- 在高于现价的网格挂做空单
- 做空单成交后补充买单减仓
- 买单成交后补充卖单开空

### 3. 双向网格
```json
{
  "enable_long_grid": true,
  "enable_short_grid": true
}
```
- 同时运行做多和做空网格
- 无论价格涨跌都能获得网格收益
- 风险对冲，降低单边风险

## 配置示例

### 只做多网格示例
```json
{
  "COIN": "BTC",
  "GRIDNUM": 10,
  "TP": 0.005,
  "EACHGRIDAMOUNT": 0.001,
  "centered": true,
  "enable_long_grid": true,
  "enable_short_grid": false
}
```

### 只做空网格示例
```json
{
  "COIN": "BTC",
  "GRIDNUM": 8,
  "TP": 0.004,
  "EACHGRIDAMOUNT": 0.001,
  "centered": true,
  "enable_long_grid": false,
  "enable_short_grid": true
}
```

### 双向网格示例
```json
{
  "COIN": "BTC",
  "GRIDNUM": 6,
  "TP": 0.003,
  "EACHGRIDAMOUNT": 0.001,
  "centered": true,
  "enable_long_grid": true,
  "enable_short_grid": true
}
``` 