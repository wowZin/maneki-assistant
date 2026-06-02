# 数据接口审计

> 所有接口清单、已知坑、权限/时序要求。更新于 2026-05-22。

## Tushare 接口 (23个)

### 行情数据

| 接口 | 用途 | 权限 | 时序 | 已知坑 |
|------|------|:--:|:--:|------|
| `daily` | 日线行情(收盘价/涨跌幅) | 基础 | T日16:00后 | T日盘中为空,复盘才能用 |
| `daily_basic` | 每日指标(换手率/量比) | 基础 | T+1 | 当天无数据 |
| `daily_info` | 交易日历 | 基础 | 实时 | - |
| `stk_factor_pro` | 复权因子/MA20 | 基础 | T日盘后 | MA20需要足够历史数据 |
| `stk_auction` | 集合竞价(开盘价/量/额) | 分钟权限 | T日9:25-9:29 | 9:25前无数据;需分钟线权限 |

### 财务数据

| 接口 | 用途 | 权限 | 时序 | 已知坑 |
|------|------|:--:|:--:|------|
| `income` | 利润表(营收/净利) | 基础 | 季/年报后 | 非财报季数据为上一期 |
| `balancesheet` | 资产负债表 | 基础 | 季/年报后 | - |
| `fina_indicator` | 财务指标(ROE/毛利率) | 基础 | 季/年报后 | - |
| `fina_forecast` | 业绩预告(扣非净利) | 基础 | 预告期 | 非预告季为空 |

### 资金/情绪

| 接口 | 用途 | 权限 | 时序 | 已知坑 |
|------|------|:--:|:--:|------|
| `moneyflow` | 个股资金流向(主力净流入) | 基础 | T+1 | 当天无实时数据 |
| `margin_detail` | 融资融券明细 | 基础 | T+1 | 当天无数据 |
| `hk_hold` | 沪深港通持股 | 基础 | T+1 | - |
| `top_list` | 龙虎榜 | 基础 | T日盘后 | 约16:30更新 |
| `top_inst` | 龙虎榜机构明细 | 基础 | T日盘后 | - |

### 涨停/封板

| 接口 | 用途 | 权限 | 时序 | 已知坑 |
|------|------|:--:|:--:|------|
| `limit_list` | 个股历史涨停记录 | 基础 | T+1 | **不支持trade_date筛选,返回全量历史** |
| `limit_list_d` | 全市场涨停列表 | 基础 | T日盘后 | 约16:00更新;盘中用daily接口替代 |
| `limit_step` | 连板天梯 | 基础 | T日盘后 | - |
| `limit_cpt_list` | 涨停概念板块(东财) | 基础 | T日盘后 | - |

### 股本/股东

| 接口 | 用途 | 权限 | 时序 | 已知坑 |
|------|------|:--:|:--:|------|
| `stock_basic` | 股票基础信息(行业) | 基础 | 静态 | **不支持`industry`参数过滤**,需全量缓存客户端筛选 |
| `share_float` | 限售解禁 | 基础 | 静态 | - |
| `stk_holdernumber` | 股东户数 | 基础 | 季/半年 | - |
| `anns_d` | 公司公告 | 基础 | 实时 | 返回JSON需解析 |
| `concept_detail` | 概念板块映射 | 概念权限 | 静态 | **需单独开通概念权限**;字段名是`concept_name`非`name` |

## 东方财富 API (2个，复用为多缓存)

> 数据优先级: **requests+代理(Eastmoney) > Tushare**。实时数据优先走东财API+代理，Tushare仅作兜底。
> 方式: **requests + zdtps代理**。代理配置: `.env` 中 `PROXY_ENABLED=true`, 模块 `scripts/proxy_utils.py`

| 接口 | 用途 | 关键字段 | 已知坑 |
|------|------|------|------|
| `push2.eastmoney.com/api/qt/clist/get` | 异动扫描/行情/资金流/人气 | f3(涨幅) f11(涨速) f62(主力净流入) f10(量比) f6(成交额) f7(换手率) | `po=0`升序/`po=1`降序;代理IP约2-3分钟过期 |
| `push2.eastmoney.com/api/qt/stock/get` | 个股实时行情(分时数据) | f170(涨跌幅) | secid格式:`{market}.{code}` |

> 三个缓存复用同一API: `_get_realtime_fund_cache`(f62/f10/f7/f6) + `_get_popularity_rank`(f62) + `_get_realtime_pct_cache`(f3)

## 关键时序窗口

```
09:25  stk_auction 竞价数据就绪 ← 不能早于此时
09:30  东财实时行情就绪
09:35  第一轮扫描开始
11:30  上午收盘,最后一轮
13:00  下午开盘
15:00  收盘
16:00  daily 日线就绪
16:30  龙虎榜 top_list 就绪
18:00  复盘开始 ← 此时所有T+1数据就绪
```

## 常见故障模式

| 症状 | 可能原因 | 检查方法 |
|------|----------|---------|
| stk_auction返回空 | 9:25前调用/无分钟权限 | 等9:26再试 |
| daily返回空 | T日盘前/盘中 | 16:00后调用 |
| moneyflow返回空 | T+1接口 | 用T-1日期 |
| concept_detail无数据 | 未开通概念权限 | Tushare控制台检查 |
| stock_basic industry过滤无效 | Tushare不支持该参数 | 客户端过滤 |
| limit_list返回超量 | 不支持日期筛选 | 取items[0]为最新 |
| 东财API超时/空 | 代理IP过期 | 重试+刷新代理 |
| call_tushare静默失败 | except:pass吞错误 | 看return是否为空dict |

## akshare 接口 (5个)

> ⚠️ akshare 底层封装东方财富API。本服务器 `push2.eastmoney.com` 被TCP层封禁，**akshare 大部分接口不可用**。

| 接口 | 用途 | 文件 | 可用? | 备注 |
|------|------|------|:--:|------|
| `ak.stock_fund_flow_individual()` | ~~全市场实时资金流向~~ | zt_pipeline.py | ❌ | 已移除,改用Tushare moneyflow |
| `ak.stock_zh_a_spot_em()` | 全市场实时行情 | scan_akshare.py | ❌ | 死代码,不被pipeline引用 |
| `ak.stock_individual_fund_flow()` | 个股资金流 | scan_akshare.py | ❌ | 死代码,不被pipeline引用 |
| `ak.stock_board_concept_name_em()` | 概念板块列表 | sentiment_analysis.py | ❌ | 死代码,不被pipeline引用 |
| `ak.stock_board_concept_cons_em()` | 概念成分股 | sentiment_analysis.py | ❌ | 死代码,不被pipeline引用 |

**结论**: akshare 全系不可用。`stock_fund_flow_individual` 已从pipeline移除，直接使用 Tushare moneyflow。其余4个为死代码，不影响运行。
