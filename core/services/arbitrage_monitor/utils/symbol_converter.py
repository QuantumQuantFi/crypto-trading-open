"""
极简符号转换器 - 专为套利监控系统设计

只做一件事：标准格式 ↔ 交易所格式转换
代码量：~100行，零冗余
"""

from typing import Dict, Optional
import logging
from pathlib import Path

try:
    import yaml
except Exception:  # pragma: no cover - yaml 为可选依赖
    yaml = None


class SimpleSymbolConverter:
    """
    极简符号转换器
    
    标准格式：BTC-USDC-PERP, ETH-USDC-PERP
    
    交易所格式：
    - Backpack: BTC_USDC_PERP
    - Lighter:  BTC
    - EdgeX:    BTCUSD
    """
    
    # 交易所格式映射（直接硬编码，避免读取配置文件）
    EXCHANGE_FORMATS = {
        'hyperliquid': {
            'separator': '/',           # BTC/USDC:USDC
            'type_separator': ':',
            'default_quote': 'USDC',
            'perp_type': 'USDC',        # Hyperliquid PERP 用 :USDC
            'spot_type': 'SPOT',
        },
        'backpack': {
            'separator': '_',
            'perp_suffix': '_PERP',
            'spot_suffix': '',
        },
        'lighter': {
            'separator': '',
            'base_only': True,  # 只返回基础币种（如 BTC）
            'perp_suffix': '',
            'spot_suffix': '',
        },
        'edgex': {
            'separator': '',
            'perp_suffix': '',  # BTCUSD（无后缀）
            'spot_suffix': '',
        },
        'paradex': {
            'separator': '-',
            'perp_suffix': '-PERP',
            'spot_suffix': '',
        },
        'binance': {
            'separator': '',
            'perp_suffix': '',          # BTCUSDT（无后缀）
            'spot_suffix': '',
            'quote_map': {'USDC': 'USDT'},
        },
        'okx': {
            'separator': '-',
            'perp_suffix': '-SWAP',     # BTC-USDT-SWAP
            'spot_suffix': '',
            'quote_map': {'USDC': 'USDT'},
        },
        'grvt': {
            'separator': '_',
            'perp_suffix': '_Perp',     # BTC_USDT_Perp
            'spot_suffix': '',
            'quote_map': {'USDC': 'USDT'},
        },
        'variational': {
            'separator': '',
            'base_only': True,          # 只用 underlying（BTC/ETH...）
            'perp_suffix': '',
            'spot_suffix': '',
        },
    }
    
    # 直接映射表（完整的12个监控symbol）
    DIRECT_MAPPING = {
        'backpack': {
            'BTC-USDC-PERP': 'BTC_USDC_PERP',
            'ETH-USDC-PERP': 'ETH_USDC_PERP',
            'SOL-USDC-PERP': 'SOL_USDC_PERP',
            'DOGE-USDC-PERP': 'DOGE_USDC_PERP',
            'AVAX-USDC-PERP': 'AVAX_USDC_PERP',
            'LINK-USDC-PERP': 'LINK_USDC_PERP',
            'UNI-USDC-PERP': 'UNI_USDC_PERP',
            'CRV-USDC-PERP': 'CRV_USDC_PERP',
            'ADA-USDC-PERP': 'ADA_USDC_PERP',
            'AAVE-USDC-PERP': 'AAVE_USDC_PERP',
            'HYPE-USDC-PERP': 'HYPE_USDC_PERP',
            'NEAR-USDC-PERP': 'NEAR_USDC_PERP',
        },
        'lighter': {
            # Lighter 使用简化符号格式（只保留基础币种）
            'BTC-USDC-PERP': 'BTC',
            'ETH-USDC-PERP': 'ETH',
            'SOL-USDC-PERP': 'SOL',
            'DOGE-USDC-PERP': 'DOGE',
            'AVAX-USDC-PERP': 'AVAX',
            'LINK-USDC-PERP': 'LINK',
            'UNI-USDC-PERP': 'UNI',
            'CRV-USDC-PERP': 'CRV',
            'ADA-USDC-PERP': 'ADA',
            'AAVE-USDC-PERP': 'AAVE',
            'HYPE-USDC-PERP': 'HYPE',
            'NEAR-USDC-PERP': 'NEAR',
        },
        'edgex': {
            'BTC-USDC-PERP': 'BTCUSD',
            'ETH-USDC-PERP': 'ETHUSD',
            'SOL-USDC-PERP': 'SOLUSD',
            'DOGE-USDC-PERP': 'DOGEUSD',
            'AVAX-USDC-PERP': 'AVAXUSD',
            'LINK-USDC-PERP': 'LINKUSD',
            'UNI-USDC-PERP': 'UNIUSD',
            'CRV-USDC-PERP': 'CRVUSD',
            'ADA-USDC-PERP': 'ADAUSD',
            'AAVE-USDC-PERP': 'AAVEUSD',
            'HYPE-USDC-PERP': 'HYPEUSD',
            'NEAR-USDC-PERP': 'NEARUSD',
        },
        'paradex': {
            # 自动转换即可，这里保留空字典占位，便于自定义映射
        },
    }
    
    _custom_mappings_loaded = False
    
    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)
        self._ensure_custom_mappings_loaded()

    @classmethod
    def _ensure_custom_mappings_loaded(cls):
        """一次性加载 config/symbol_conversion.yaml 中的自定义映射"""
        if cls._custom_mappings_loaded:
            return
        cls._custom_mappings_loaded = True  # 默认视为已加载，避免重复尝试

        config_path = Path("config/symbol_conversion.yaml")
        if not config_path.exists():
            return

        if yaml is None:
            logging.getLogger(__name__).warning(
                "⚠️ 未安装 PyYAML，跳过自定义符号映射加载: %s", config_path
            )
            return

        try:
            with config_path.open("r", encoding="utf-8") as f:
                config_data = yaml.safe_load(f) or {}
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "⚠️ 读取 %s 失败，跳过自定义符号映射: %s", config_path, exc
            )
            return

        symbol_mappings = (
            config_data.get("symbol_mappings", {}).get("standard_to_exchange", {})
        )
        if not isinstance(symbol_mappings, dict):
            return

        for exchange, mappings in symbol_mappings.items():
            if not isinstance(mappings, dict):
                continue
            cls.DIRECT_MAPPING.setdefault(exchange, {})
            # 直接覆盖/更新，yaml 中的定义优先级最高
            cls.DIRECT_MAPPING[exchange].update(mappings)

    
    def convert_to_exchange(self, standard_symbol: str, exchange: str) -> str:
        """
        标准格式 -> 交易所格式
        
        Args:
            standard_symbol: 标准格式（如 BTC-USDC-PERP）
            exchange: 交易所名称（如 'backpack'）
            
        Returns:
            交易所格式符号
        """
        exchange = exchange.lower()
        
        # 1. 优先使用直接映射表
        if exchange in self.DIRECT_MAPPING:
            if standard_symbol in self.DIRECT_MAPPING[exchange]:
                result = self.DIRECT_MAPPING[exchange][standard_symbol]
                self.logger.debug(f"🔄 直接映射: {standard_symbol} -> {result} ({exchange})")
                return result
        
        # 2. 如果没有映射，尝试自动转换
        if exchange not in self.EXCHANGE_FORMATS:
            self.logger.warning(f"⚠️  不支持的交易所 {exchange}，返回原始符号")
            return standard_symbol
        
        try:
            result = self._auto_convert(standard_symbol, exchange)
            self.logger.debug(f"🔄 自动转换: {standard_symbol} -> {result} ({exchange})")
            return result
        except Exception as e:
            self.logger.error(f"❌ 转换失败 {standard_symbol} -> {exchange}: {e}")
            return standard_symbol
    
    def _auto_convert(self, standard_symbol: str, exchange: str) -> str:
        """自动转换逻辑"""
        # 解析标准格式：BTC-USDC-PERP -> ['BTC', 'USDC', 'PERP']
        parts = standard_symbol.split('-')
        if len(parts) < 2:
            return standard_symbol
        
        base = parts[0]  # BTC
        quote = parts[1] if len(parts) > 1 else 'USDC'  # USDC
        market_type = parts[2] if len(parts) > 2 else 'SPOT'  # PERP
        
        fmt = self.EXCHANGE_FORMATS[exchange]
        
        # 特殊处理：Lighter 只返回基础币种
        if fmt.get('base_only'):
            return base

        # Hyperliquid：BTC/USDC:USDC（永续）
        if exchange == 'hyperliquid':
            quote = fmt.get('default_quote', quote) or quote
            if market_type == 'PERP':
                typ = fmt.get('perp_type', 'USDC')
                return f"{base}/{quote}:{typ}"
            if market_type == 'SPOT':
                typ = fmt.get('spot_type', 'SPOT')
                return f"{base}/{quote}:{typ}"
            return f"{base}/{quote}"

        # Binance：为了让永续合约走期货 WS（单连接 + SUBSCRIBE 多流），
        # PERP 输出统一使用 ccxt 兼容格式：BTC/USDT:USDT
        if exchange == 'binance' and market_type == 'PERP':
            quote_map = fmt.get('quote_map') or {}
            if quote in quote_map:
                quote = quote_map[quote]
            return f"{base}/{quote}:{quote}"

        # quote 映射（USDC -> USDT / USD 等）
        quote_map = fmt.get('quote_map') or {}
        if quote in quote_map:
            quote = quote_map[quote]
        
        # 🔥 特殊处理：EdgeX使用USD作为quote，而不是USDC
        if exchange in ('edgex', 'paradex'):
            if quote == 'USDC':
                quote = 'USD'
        
        # 构造交易所格式
        separator = fmt['separator']
        suffix = fmt['perp_suffix'] if market_type == 'PERP' else fmt['spot_suffix']
        
        # 组装
        if separator:
            result = f"{base}{separator}{quote}{suffix}"
        else:
            result = f"{base}{quote}{suffix}"
        
        return result
    
    def convert_from_exchange(self, exchange_symbol: str, exchange: str) -> str:
        """
        交易所格式 -> 标准格式（反向转换）
        
        Args:
            exchange_symbol: 交易所格式符号（如 'BTC', 'BTCUSD', 'BTC_USDC_PERP'）
            exchange: 交易所名称（如 'lighter'）
            
        Returns:
            标准格式符号（如 'BTC-USDC-PERP'）
        """
        exchange = exchange.lower()
        
        # 1. 构建反向映射表（懒加载）
        if not hasattr(self, '_reverse_mapping'):
            self._reverse_mapping = {}
            for ex, mappings in self.DIRECT_MAPPING.items():
                self._reverse_mapping[ex] = {v: k for k, v in mappings.items()}
        
        # 2. 查找反向映射
        if exchange in self._reverse_mapping:
            if exchange_symbol in self._reverse_mapping[exchange]:
                result = self._reverse_mapping[exchange][exchange_symbol]
                self.logger.debug(f"🔄 反向映射: {exchange_symbol} -> {result} ({exchange})")
                return result
        
        # 3. 如果没有找到，使用自动推断（降低日志级别为 DEBUG）
        self.logger.debug(f"🔄 未找到反向映射: {exchange_symbol} ({exchange})，尝试自动推断")
        
        # 4. 尝试自动推断（基于交易所格式）
        if exchange == 'lighter':
            # Lighter: BTC -> BTC-USDC-PERP
            return f"{exchange_symbol}-USDC-PERP"
        elif exchange == 'edgex':
            # EdgeX: BTCUSD -> BTC-USDC-PERP
            if exchange_symbol.endswith('USD'):
                base = exchange_symbol[:-3]  # 去掉 'USD'
                return f"{base}-USDC-PERP"
        elif exchange == 'backpack':
            # Backpack: BTC_USDC_PERP -> BTC-USDC-PERP
            return exchange_symbol.replace('_', '-')
        elif exchange == 'paradex':
            parts = exchange_symbol.split('-')
            if len(parts) >= 3:
                base = parts[0]
                quote = parts[1]
                market_type = parts[2]
                if quote == 'USD':
                    quote = 'USDC'
                return f"{base}-{quote}-{market_type}"
            # 当频道返回 ALL 等特殊字符串时，直接回传
            return exchange_symbol
        elif exchange == 'hyperliquid':
            # Hyperliquid: BTC/USDC:USDC -> BTC-USDC-PERP
            sym = exchange_symbol.strip()
            if '/' in sym:
                base_part, rest = sym.split('/', 1)
                quote = rest
                typ = None
                if ':' in rest:
                    quote, typ = rest.split(':', 1)
                quote = quote.upper()
                if quote == 'USD':
                    quote = 'USDC'
                if not typ:
                    return f"{base_part.upper()}-{quote}-SPOT"
                typ_u = typ.upper()
                if typ_u in ('USDC', 'PERP', 'SWAP'):
                    return f"{base_part.upper()}-{quote}-PERP"
                if typ_u == 'SPOT':
                    return f"{base_part.upper()}-{quote}-SPOT"
                return f"{base_part.upper()}-{quote}-{typ_u}"
        elif exchange == 'binance':
            # Binance: BTCUSDT -> BTC-USDC-PERP
            sym = exchange_symbol.strip().upper()
            # 兼容期货/永续格式：BTC/USDT:USDT
            if "/" in sym:
                base_part, rest = sym.split("/", 1)
                quote_part = rest
                if ":" in rest:
                    quote_part, _typ = rest.split(":", 1)
                quote_part = quote_part.upper()
                if quote_part == "USDT":
                    quote_part = "USDC"
                return f"{base_part.upper()}-{quote_part}-PERP"
            for q in ('USDT', 'USDC', 'BUSD'):
                if sym.endswith(q) and len(sym) > len(q):
                    base = sym[: -len(q)]
                    return f"{base}-USDC-PERP"
        elif exchange == 'okx':
            # OKX: BTC-USDT-SWAP -> BTC-USDC-PERP
            sym = exchange_symbol.strip().upper()
            parts = sym.split('-')
            if len(parts) >= 3 and parts[-1] == 'SWAP':
                base = parts[0]
                quote = parts[1]
                if quote == 'USDT':
                    quote = 'USDC'
                return f"{base}-{quote}-PERP"
        elif exchange == 'grvt':
            # GRVT: BTC_USDT_Perp -> BTC-USDC-PERP
            sym = exchange_symbol.strip().replace('-', '_').replace('/', '_')
            parts = sym.split('_')
            if len(parts) >= 3:
                base = parts[0].upper()
                quote = parts[1].upper()
                typ_raw = parts[2]
                typ = typ_raw.upper()
                if quote == 'USDT':
                    quote = 'USDC'
                # 处理 Perp / PERP / PERPETUAL
                if typ in ('PERP', 'PERPETUAL') or typ_raw.lower() == 'perp' or typ.startswith('PERP'):
                    return f"{base}-{quote}-PERP"
                return f"{base}-{quote}-{typ}"
        elif exchange == 'variational':
            # Variational: BTC -> BTC-USDC-PERP
            sym = exchange_symbol.strip().upper()
            if sym and sym.isalnum():
                return f"{sym}-USDC-PERP"
        
        # 5. 无法推断，返回原始符号
        return exchange_symbol
    
    def add_mapping(self, exchange: str, standard_symbol: str, exchange_symbol: str):
        """
        运行时添加映射（用于用户自定义）
        
        Args:
            exchange: 交易所名称
            standard_symbol: 标准格式符号
            exchange_symbol: 交易所格式符号
        """
        exchange = exchange.lower()
        if exchange not in self.DIRECT_MAPPING:
            self.DIRECT_MAPPING[exchange] = {}
        self.DIRECT_MAPPING[exchange][standard_symbol] = exchange_symbol
        
        # 清除反向映射缓存
        if hasattr(self, '_reverse_mapping'):
            delattr(self, '_reverse_mapping')
        
        self.logger.info(f"✅ 添加映射: {standard_symbol} -> {exchange_symbol} ({exchange})")
    
    def get_supported_exchanges(self) -> list:
        """获取支持的交易所列表"""
        return list(self.EXCHANGE_FORMATS.keys())
