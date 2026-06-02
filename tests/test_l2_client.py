"""tests for scripts/l2_client.py — 纯函数直接用真实数据测，网络部分用结构验证"""

import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.l2_client import (
    normalize_code, to_price, to_volume,
    parse_market_record, parse_order_record, parse_tran_record,
    parse_payload, MinuteKlineAggregator,
)


class TestNormalizeCode(unittest.TestCase):
    def test_sh_prefix(self):
        self.assertEqual(normalize_code("600519"), "600519.SH")
        self.assertEqual(normalize_code("688001"), "688001.SH")
        self.assertEqual(normalize_code("601318"), "601318.SH")
        self.assertEqual(normalize_code("603259"), "603259.SH")

    def test_sz_prefix(self):
        self.assertEqual(normalize_code("000001"), "000001.SZ")
        self.assertEqual(normalize_code("300750"), "300750.SZ")
        self.assertEqual(normalize_code("002415"), "002415.SZ")
        self.assertEqual(normalize_code("000858"), "000858.SZ")

    def test_already_suffixed(self):
        self.assertEqual(normalize_code("600519.SH"), "600519.SH")
        self.assertEqual(normalize_code("000001.SZ"), "000001.SZ")
        self.assertEqual(normalize_code("688001.SH"), "688001.SH")


class TestPriceVolume(unittest.TestCase):
    def test_to_price_normal(self):
        self.assertAlmostEqual(to_price("123456"), 12.3456)
        self.assertAlmostEqual(to_price("50000"), 5.0)

    def test_to_price_edge(self):
        self.assertEqual(to_price("0"), 0.0)
        self.assertEqual(to_price(""), 0.0)
        self.assertEqual(to_price(None), 0.0)
        self.assertEqual(to_price("abc"), 0.0)

    def test_to_volume_normal(self):
        self.assertEqual(to_volume("1000"), 1000)
        self.assertEqual(to_volume("0"), 0)
        self.assertEqual(to_volume(""), 0)
        self.assertEqual(to_volume("abc"), 0)

    def test_roundtrip_price_conversion(self):
        """price字段×10000整数 ↔ float元的往返一致性"""
        raw_price = "123456"
        price = to_price(raw_price)
        self.assertAlmostEqual(price * 10000, 123456.0)


class TestParseMarketRecord(unittest.TestCase):
    def test_real_like_record(self):
        """模拟真实Market推送包的解析"""
        # 构造一个66字段的模拟记录
        fields = [
            "1",          # 0: pack_no
            "SH",         # 1: market_code
            "600519",     # 2: symbol
            "20240101",   # 3: trade_date
            "093000",     # 4: time
            "T",          # 5: status
            "180000",     # 6: prev_close
            "181000",     # 7: open
            "185000",     # 8: high
            "179000",     # 9: low
            "182500",     # 10: last
            # ask_price[10]
            "183000", "184000", "185000", "186000", "187000",
            "188000", "189000", "190000", "191000", "192000",
            # ask_qty[10]
            "1000", "2000", "3000", "4000", "5000",
            "6000", "7000", "8000", "9000", "10000",
            # bid_price[10]
            "181000", "180000", "179000", "178000", "177000",
            "176000", "175000", "174000", "173000", "172000",
            # bid_qty[10]
            "500", "600", "700", "800", "900",
            "1000", "1100", "1200", "1300", "1400",
            "5000",       # 51: trade_count
            "1000000",    # 52: trade_volume
            "180000000",  # 53: trade_amount
            "50000",      # 54: total_bid_volume
            "30000",      # 55: total_ask_volume
            "180500",     # 56: avg_bid_price
            "185500",     # 57: avg_ask_price
            "198000",     # 58: limit_up
            "162000",     # 59: limit_down
            "10000",      # 60: total_buy_orders
            "8000",       # 61: total_sell_orders
            "200",        # 62: buy_cancel_orders
            "10000",      # 63: buy_cancel_volume
            "150",        # 64: sell_cancel_orders
            "8000",       # 65: sell_cancel_volume
        ]
        rec = parse_market_record(fields)
        self.assertEqual(rec["symbol"], "600519")
        self.assertEqual(rec["last"], "182500")
        self.assertEqual(len(rec["ask_price"]), 10)
        self.assertEqual(rec["ask_price"][0], "183000")
        self.assertEqual(rec["bid_price"][0], "181000")
        self.assertEqual(rec["ask_qty"][0], "1000")
        self.assertEqual(rec["bid_qty"][0], "500")

    def test_incomplete_fields(self):
        """字段不足时缺失字段用空字符串填充"""
        rec = parse_market_record(["0", "SH", "600519"])
        self.assertEqual(rec["symbol"], "600519")
        self.assertEqual(rec["last"], "")
        self.assertEqual(rec["ask_price"][0], "")


class TestParseOrderRecord(unittest.TestCase):
    def test_real_like_record(self):
        fields = [
            "1", "SH", "600519", "20240101", "093005",
            "ORD123456", "182500", "500", "0", "B",
            "ORIG001", "SEQ001", "CH1"
        ]
        rec = parse_order_record(fields)
        self.assertEqual(rec["order_no"], "ORD123456")
        self.assertEqual(rec["order_price"], "182500")
        self.assertEqual(rec["order_qty"], "500")
        self.assertEqual(rec["order_bs"], "B")


class TestParseTranRecord(unittest.TestCase):
    def test_real_like_record(self):
        fields = [
            "1", "SH", "600519", "20240101", "093005",
            "TR123456", "182500", "100", "1825000", "B",
            "0", "ORIG001", "ASEQ001", "BSEQ002"
        ]
        rec = parse_tran_record(fields)
        self.assertEqual(rec["trade_no"], "TR123456")
        self.assertEqual(rec["trade_price"], "182500")
        self.assertEqual(rec["trade_qty"], "100")
        self.assertEqual(rec["bs_flag"], "B")


class TestParsePayload(unittest.TestCase):
    def test_market_payload_single(self):
        """解析单条Market数据"""
        raw = (
            "0,SH,600519,20240101,093000,T,180000,181000,185000,179000,182500,"
            + ",".join(["0"] * 55)
        )
        records = parse_payload("Market", raw)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["symbol"], "600519")

    def test_empty_payload(self):
        self.assertEqual(parse_payload("Market", ""), [])
        self.assertEqual(parse_payload("Market", "   "), [])
        self.assertEqual(parse_payload("Order", ""), [])

    def test_unknown_type(self):
        self.assertEqual(parse_payload("Unknown", "some,data"), [])

    def test_order_payload(self):
        raw = "0,SH,600519,20240101,093005,ORD001,182500,500,0,B,ORIG,SQ,CH"
        records = parse_payload("Order", raw)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["order_no"], "ORD001")

    def test_tran_payload(self):
        raw = "0,SH,600519,20240101,093005,TR001,182500,100,1825000,B,0,ORIG,ASQ,BSQ"
        records = parse_payload("Tran", raw)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["trade_no"], "TR001")

    def test_parse_payload_no_crash_on_any_string(self):
        """任意字符串输入不抛异常（过滤逻辑在_process_data层，不在这里）"""
        raw = "HeartBeat"
        records = parse_payload("Market", raw)
        # parse_payload 不负责过滤，输入啥解析啥
        self.assertIsInstance(records, list)
        # 清理: 空字符串替换为占位符，确保后续不崩
        for r in records:
            self.assertIsInstance(r, dict)


class TestMinuteKlineAggregator(unittest.TestCase):
    def setUp(self):
        self.agg = MinuteKlineAggregator(max_bars=240)

    def _make_tran(self, time, trade_price, trade_qty, trade_amount):
        return {
            "time": time,
            "trade_price": str(trade_price),
            "trade_qty": str(trade_qty),
            "trade_amount": str(trade_amount),
        }

    def test_single_bar(self):
        self.agg.feed("000001.SZ", self._make_tran("0930", 100000, 100, 10000))
        bars = self.agg.get_bars("000001.SZ")
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["time"], "0930")
        self.assertAlmostEqual(bars[0]["open"], 10.0)
        self.assertAlmostEqual(bars[0]["close"], 10.0)

    def test_same_minute_aggregates(self):
        self.agg.feed("000001.SZ", self._make_tran("0930", 100000, 100, 10000))
        self.agg.feed("000001.SZ", self._make_tran("0930", 110000, 200, 22000))
        bars = self.agg.get_bars("000001.SZ")
        self.assertEqual(len(bars), 1)
        self.assertAlmostEqual(bars[0]["high"], 11.0)
        self.assertAlmostEqual(bars[0]["low"], 10.0)
        self.assertAlmostEqual(bars[0]["close"], 11.0)
        self.assertEqual(bars[0]["volume"], 300)

    def test_multiple_minutes(self):
        self.agg.feed("000001.SZ", self._make_tran("0930", 100000, 100, 10000))
        self.agg.feed("000001.SZ", self._make_tran("0931", 110000, 200, 22000))
        bars = self.agg.get_bars("000001.SZ")
        self.assertEqual(len(bars), 2)

    def test_vwap_calculation(self):
        self.agg.feed("000001.SZ", self._make_tran("0930", 100000, 100, 10000))
        self.agg.feed("000001.SZ", self._make_tran("0931", 200000, 100, 20000))
        # amount/to_price=1.0+2.0, volume=100+100, vwap=3.0/200=0.015
        vwap = self.agg.get_vwap("000001.SZ")
        self.assertAlmostEqual(vwap, 0.015)

    def test_vwap_zero_volume(self):
        self.assertEqual(self.agg.get_vwap("NOEXIST"), 0.0)

    def test_max_bars_truncation(self):
        agg = MinuteKlineAggregator(max_bars=3)
        for i in range(5):
            minute = f"09{30 + i}"
            agg.feed("000001.SZ", self._make_tran(minute, 100000, 100, 10000))
        self.assertEqual(len(agg.get_bars("000001.SZ")), 3)

    def test_get_bars_with_n(self):
        for i in range(5):
            minute = f"09{30 + i}"
            self.agg.feed("000001.SZ", self._make_tran(minute, 100000, 100, 10000))
        self.assertEqual(len(self.agg.get_bars("000001.SZ", n=2)), 2)

    def test_clear_symbol(self):
        self.agg.feed("000001.SZ", self._make_tran("0930", 100000, 100, 10000))
        self.agg.clear_symbol("000001.SZ")
        self.assertEqual(len(self.agg.get_bars("000001.SZ")), 0)

    def test_zero_price_ignored(self):
        self.agg.feed("000001.SZ", self._make_tran("0930", 0, 100, 0))
        self.assertEqual(len(self.agg.get_bars("000001.SZ")), 0)

    def test_short_time_ignored(self):
        self.agg.feed("000001.SZ", self._make_tran("09", 100000, 100, 10000))
        self.assertEqual(len(self.agg.get_bars("000001.SZ")), 0)


if __name__ == "__main__":
    unittest.main()