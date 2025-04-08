import os
import uuid
import asyncio
import aiohttp
import logging
import json
import numpy as np
import uvicorn
from datetime import datetime, timedelta
from typing import Dict, List, Any, Tuple, Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator
from functools import wraps
from pytz import timezone
import traceback
import time

# Constants
DEFAULT_RISK_PERCENTAGE = 15.0  # 15% risk per trade
MAX_RISK_PERCENTAGE = 20.0      # Maximum 20% risk per trade
MAX_DAILY_LOSS = 0.5          # Maximum 5% daily loss
POSITION_SIZE_MULTIPLIER = 100000  # Standard lot multiplier
INSTRUMENT_MULTIPLIERS = {
    'FOREX': 1.0,
    'FOREX_JPY': 0.01,
    'METAL': 0.1,
    'INDEX': 0.1,
    'ENERGY': 0.1
}

# Timeframe-specific settings
TIMEFRAME_TAKE_PROFIT_LEVELS = {
    '1m': {'first_exit': 0.3, 'second_exit': 0.3, 'runner': 0.4},
    '5m': {'first_exit': 0.3, 'second_exit': 0.3, 'runner': 0.4},
    '15m': {'first_exit': 0.25, 'second_exit': 0.35, 'runner': 0.4},
    '30m': {'first_exit': 0.25, 'second_exit': 0.35, 'runner': 0.4},
    '1H': {'first_exit': 0.2, 'second_exit': 0.4, 'runner': 0.4},
    '4H': {'first_exit': 0.2, 'second_exit': 0.3, 'runner': 0.5},
    'D': {'first_exit': 0.15, 'second_exit': 0.25, 'runner': 0.6}
}

TIMEFRAME_TIME_STOPS = {
    '1m': {'optimal_duration': 15/60, 'max_duration': 30/60, 'stop_adjustment': 0.5},
    '5m': {'optimal_duration': 1, 'max_duration': 2, 'stop_adjustment': 0.5},
    '15m': {'optimal_duration': 3, 'max_duration': 6, 'stop_adjustment': 0.5},
    '30m': {'optimal_duration': 6, 'max_duration': 12, 'stop_adjustment': 0.6},
    '1H': {'optimal_duration': 12, 'max_duration': 24, 'stop_adjustment': 0.7},
    '4H': {'optimal_duration': 24, 'max_duration': 48, 'stop_adjustment': 0.8},
    'D': {'optimal_duration': 72, 'max_duration': 120, 'stop_adjustment': 0.8}
}

TIMEFRAME_TRAILING_SETTINGS = {
    '1m': {
        'initial_multiplier': 1.0,
        'profit_levels': [
            {'threshold': 1.0, 'multiplier': 0.7},
            {'threshold': 2.0, 'multiplier': 0.5},
            {'threshold': 3.0, 'multiplier': 0.3}
        ]
    },
    '5m': {
        'initial_multiplier': 1.0,
        'profit_levels': [
            {'threshold': 1.0, 'multiplier': 0.7},
            {'threshold': 2.0, 'multiplier': 0.5},
            {'threshold': 3.0, 'multiplier': 0.3}
        ]
    },
    '15m': {
        'initial_multiplier': 1.0,
        'profit_levels': [
            {'threshold': 1.0, 'multiplier': 0.8},
            {'threshold': 2.0, 'multiplier': 0.6},
            {'threshold': 3.0, 'multiplier': 0.4}
        ]
    },
    '30m': {
        'initial_multiplier': 1.0,
        'profit_levels': [
            {'threshold': 1.0, 'multiplier': 0.8},
            {'threshold': 2.0, 'multiplier': 0.6},
            {'threshold': 3.0, 'multiplier': 0.4}
        ]
    },
    '1H': {
        'initial_multiplier': 1.0,
        'profit_levels': [
            {'threshold': 1.0, 'multiplier': 0.8},
            {'threshold': 2.0, 'multiplier': 0.6},
            {'threshold': 3.0, 'multiplier': 0.4}
        ]
    },
    '4H': {
        'initial_multiplier': 1.0,
        'profit_levels': [
            {'threshold': 1.0, 'multiplier': 0.8},
            {'threshold': 2.0, 'multiplier': 0.5},
            {'threshold': 3.0, 'multiplier': 0.3}
        ]
    },
    'D': {
        'initial_multiplier': 1.0,
        'profit_levels': [
            {'threshold': 1.0, 'multiplier': 0.8},
            {'threshold': 2.0, 'multiplier': 0.5},
            {'threshold': 3.0, 'multiplier': 0.3}
        ]
    }
}

# Error handling
class TradingError(Exception):
    """Base exception for trading errors"""
    pass

class MarketError(TradingError):
    """Exception for market-related errors"""
    pass

class OrderError(TradingError):
    """Exception for order-related errors"""
    pass

# Error handling decorators
def handle_errors(func):
    """Decorator for handling synchronous function errors"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.error(f"Error in {func.__name__}: {str(e)}")
            logger.error(traceback.format_exc())
            raise
    return wrapper

# Error handling decorators
def handle_errors(func):
    """Decorator for handling synchronous function errors"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.error(f"Error in {func.__name__}: {str(e)}")
            logger.error(traceback.format_exc())
            raise
    return wrapper

def handle_async_errors(func):
    """Decorator for handling asynchronous function errors"""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            logger.error(f"Error in {func.__name__}: {str(e)}")
            logger.error(traceback.format_exc())
            raise
    return wrapper

# First, define the Settings class
class Settings:
    """Configuration settings for the trading bot"""
    
    def __init__(self):
        # API settings
        self.oanda_api_url = os.getenv("OANDA_API_URL", "https://api-fxpractice.oanda.com")
        # Remove trailing /v3 if present to avoid duplication
        if self.oanda_api_url.endswith('/v3'):
            self.oanda_api_url = self.oanda_api_url[:-3]  # Remove trailing /v3
        self.oanda_account = os.getenv("OANDA_ACCOUNT", "")
        self.oanda_api_key = os.getenv("OANDA_API_KEY", "")
        
        # Timeouts
        self.session_timeout = int(os.getenv("SESSION_TIMEOUT", "30"))
        self.request_timeout = int(os.getenv("REQUEST_TIMEOUT", "10"))
        
        # Trading limits
        self.max_positions = int(os.getenv("MAX_POSITIONS", "10"))
        self.max_correlation = float(os.getenv("MAX_CORRELATION", "0.8"))
        self.max_daily_trades = int(os.getenv("MAX_DAILY_TRADES", "100"))

# Then instantiate the Settings class
config = Settings()

# Logging
class JsonFormatter(logging.Formatter):
    """JSON formatter for structured logging"""
    
    def format(self, record):
        log_data = {
            "timestamp": datetime.now().isoformat(),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
        }
        
        if hasattr(record, "request_id"):
            log_data["request_id"] = record.request_id
            
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
            
        return json.dumps(log_data)

def config_logging():
    """Configure logging"""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    
    # Remove default handlers
    for h in logger.handlers[:]:
        if not isinstance(h, logging.StreamHandler) or h is not handler:
            logger.removeHandler(h)

logger = logging.getLogger(__name__)

# Models
class AlertData(BaseModel):
    """Model for incoming alert data"""
    symbol: str
    action: str  # BUY, SELL, CLOSE, UPDATE
    timeframe: str = '1H'
    risk_percentage: Optional[float] = None
    account: Optional[str] = None
    percentage: Optional[float] = None  # For partial closes
    position_update: Optional[Dict[str, Any]] = None  # For updates
    
    @field_validator('symbol')
    def validate_symbol(cls, v):
        """Validate symbol format"""
        if not v or len(v) < 3:
            raise ValueError("Symbol must be at least 3 characters")
        return v
    
    @field_validator('action')
    def validate_action(cls, v):
        """Validate action"""
        valid_actions = ['BUY', 'SELL', 'CLOSE', 'UPDATE']
        if v not in valid_actions:
            raise ValueError(f"Action must be one of {valid_actions}")
        return v
    
    @field_validator('percentage')
    def validate_percentage(cls, v):
        """Validate percentage for partial closes"""
        if v is not None and (v <= 0 or v >= 100):
            raise ValueError("Percentage must be between 0 and 100")
        return v

# Database integration (placeholder)
async def init_db():
    """Initialize database connection"""
    logger.info("Database initialized")

async def close_db_connection():
    """Close database connection"""
    logger.info("Database connection closed")

# Session management
_session_store = {}

async def get_session() -> aiohttp.ClientSession:
    """Get or create a session"""
    # Get current task id
    task_id = id(asyncio.current_task())
    
    # Check if we have a session for this task
    if task_id in _session_store:
        session_data = _session_store[task_id]
        
        # Check if session is still valid
        if session_data["timestamp"] + config.session_timeout > time.time():
            # Update timestamp
            session_data["timestamp"] = time.time()
            return session_data["session"]
        else:
            # Close old session before creating a new one
            try:
                await _session_store[task_id]["session"].close()
            except:
                pass
            del _session_store[task_id]
    
    # Create new session
    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=config.request_timeout)
    )
    
    # Store in session store
    _session_store[task_id] = {
        "session": session,
        "timestamp": time.time()
    }
    
    return session

async def close_all_sessions():
    """Close all active sessions"""
    tasks = []
    
    for task_id, session_data in list(_session_store.items()):
        session = session_data["session"]
        tasks.append(asyncio.create_task(session.close()))
        del _session_store[task_id]
    
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    
    logger.info(f"Closed {len(tasks)} sessions")

async def cleanup_stale_sessions():
    """Cleanup stale sessions"""
    now = time.time()
    stale_tasks = []
    
    for task_id, session_data in list(_session_store.items()):
        if session_data["timestamp"] + config.session_timeout < now:
            session = session_data["session"]
            stale_tasks.append(asyncio.create_task(session.close()))
            del _session_store[task_id]
    
    if stale_tasks:
        await asyncio.gather(*stale_tasks, return_exceptions=True)
        logger.info(f"Cleaned up {len(stale_tasks)} stale sessions")

async def cleanup_sessions():
    """Periodically clean up stale sessions"""
    while True:
        try:
            await asyncio.sleep(60)  # Run every minute
            await cleanup_stale_sessions()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in session cleanup: {str(e)}")
            await asyncio.sleep(30)  # Short delay before retry

# API implementation with lifespan
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await init_db()
    await position_manager.start()
    cleanup_task = asyncio.create_task(cleanup_sessions())
    
    yield
    
    # Shutdown
    cleanup_task.cancel()
    await position_manager.stop()
    await close_db_connection()
    await close_all_sessions()

app = FastAPI(title="Trading Bot API", version="1.0.0", lifespan=lifespan)

# Market Analysis Classes
class VolatilityMonitor:
    """Monitors and analyzes volatility without maintaining its own state"""
    
    def initialize_volatility_tracking(self, symbol: str, timeframe: str) -> Dict[str, Any]:
        """Initialize volatility tracking data for a symbol"""
        return {
            'price_history': [],
            'volatility_history': [],
            'current_volatility_state': 'NORMAL',
            'volatility_state_history': []
        }
    
    def update_volatility(self, symbol: str, current_price: float, 
                          timeframe: str, regime_data: Dict[str, Any]) -> Dict[str, Any]:
        """Update volatility data based on current price"""
        # Initialize volatility history if not present
        if 'volatility_history' not in regime_data:
            regime_data['volatility_history'] = []
            regime_data['current_volatility_state'] = 'NORMAL'
            regime_data['volatility_state_history'] = []
            
        # Need at least 2 prices to calculate volatility
        price_history = regime_data['price_history']
        if len(price_history) < 2:
            return regime_data
            
        # Calculate percentage change
        prev_price = price_history[-2]
        pct_change = abs((current_price - prev_price) / prev_price)
        
        # Add to volatility history
        regime_data['volatility_history'].append(pct_change)
        
        # Keep last 20 volatility values
        if len(regime_data['volatility_history']) > 20:
            regime_data['volatility_history'].pop(0)
            
        # Determine volatility state if we have enough data
        if len(regime_data['volatility_history']) >= 5:
            avg_vol = sum(regime_data['volatility_history']) / len(regime_data['volatility_history'])
            
            # Calculate standard deviation
            variance = sum([(x - avg_vol) ** 2 for x in regime_data['volatility_history']]) / len(regime_data['volatility_history'])
            std_dev = variance ** 0.5
            
            # Set thresholds based on standard deviation
            high_threshold = avg_vol + std_dev * 1.5
            low_threshold = max(0, avg_vol - std_dev * 0.5)  # Ensure non-negative
            
            # Classify current volatility
            current_vol = regime_data['volatility_history'][-1]
            
            if current_vol > high_threshold:
                vol_state = 'HIGH'
            elif current_vol < low_threshold:
                vol_state = 'LOW'
            else:
                vol_state = 'NORMAL'
                
            # Update state
            regime_data['current_volatility_state'] = vol_state
            regime_data['volatility_state_history'].append(vol_state)
            
            # Keep last 10 states
            if len(regime_data['volatility_state_history']) > 10:
                regime_data['volatility_state_history'].pop(0)
                
        return regime_data
        
    def should_adjust_risk(self, regime_data: Dict[str, Any]) -> Tuple[bool, float]:
        """Determine if risk parameters should be adjusted based on volatility"""
        if 'current_volatility_state' not in regime_data:
            return False, 1.0
            
        volatility_state = regime_data['current_volatility_state']
        
        # Check if we have a history of volatility states
        vol_history = regime_data.get('volatility_state_history', [])
        if len(vol_history) < 3:
            return False, 1.0
            
        # Check recent states (last 3)
        recent_states = vol_history[-3:]
        
        # If consistently high volatility, tighten risk parameters
        if recent_states.count('HIGH') >= 2:
            return True, 0.8  # Reduce risk by 20%
            
        # If consistently low volatility, may loosen risk parameters
        if recent_states.count('LOW') >= 2:
            return True, 1.2  # Increase risk by 20%
            
        return False, 1.0  # No adjustment needed

class LorentzianDistanceClassifier:
    """Classifies market regimes using Lorentzian distance metrics without maintaining state"""
    
    def calculate_lorentzian_distance(self, current_price: float, price_history: List[float]) -> List[float]:
        """Calculate Lorentzian distance between current price and price history"""
        distances = []
        for hist_price in price_history:
            # Lorentzian distance formula: ln(1 + |x - y|)
            distance = np.log(1 + abs(current_price - hist_price))
            distances.append(distance)
            
        return distances
        
    def classify_market_regime(self, current_price: float, regime_data: Dict[str, Any]) -> str:
        """Classify market regime based on price action"""
        price_history = regime_data.get('price_history', [])
        
        if len(price_history) < 5:
            return 'UNKNOWN'
            
        # Calculate Lorentzian distance
        distances = self.calculate_lorentzian_distance(current_price, price_history[:-1])
        avg_distance = sum(distances) / len(distances)
        
        # Calculate momentum
        short_term_momentum = current_price / price_history[-2] - 1 if len(price_history) >= 2 else 0
        medium_term_momentum = current_price / price_history[-5] - 1 if len(price_history) >= 5 else 0
        
        # Get volatility info
        volatility_state = regime_data.get('current_volatility_state', 'NORMAL')
        
        # Classify regime
        if volatility_state == 'HIGH' and avg_distance > 0.01:
            return 'VOLATILE'
        elif avg_distance < 0.002:
            return 'RANGING'
        elif medium_term_momentum > 0.01 and short_term_momentum > 0.001:
            return 'TRENDING_UP'
        elif medium_term_momentum < -0.01 and short_term_momentum < -0.001:
            return 'TRENDING_DOWN'
        elif abs(short_term_momentum) > 0.005:
            return 'MOMENTUM'
        else:
            return 'NEUTRAL'
            
    def get_regime_adjustment_factors(self, regime: str) -> Dict[str, float]:
        """Get adjustment factors based on market regime"""
        adjustments = {
            'stop_loss': 1.0,
            'take_profit': 1.0,
            'trailing_stop': 1.0
        }
        
        if regime == 'VOLATILE':
            # Widen stops, bring take profits closer
            adjustments['stop_loss'] = 1.2
            adjustments['take_profit'] = 0.8
            adjustments['trailing_stop'] = 1.2
        elif regime == 'RANGING':
            # Narrower stops and take profits
            adjustments['stop_loss'] = 0.8
            adjustments['take_profit'] = 0.8
            adjustments['trailing_stop'] = 0.8
        elif regime in ['TRENDING_UP', 'TRENDING_DOWN']:
            # Wider stops, extended take profits
            adjustments['stop_loss'] = 1.1
            adjustments['take_profit'] = 1.2
            adjustments['trailing_stop'] = 0.9
        elif regime == 'MOMENTUM':
            # Standard stops, extended take profits
            adjustments['stop_loss'] = 1.0
            adjustments['take_profit'] = 1.1
            adjustments['trailing_stop'] = 0.9
            
        return adjustments

# Position Manager
# Forward declarations for functions used by PositionManager
async def get_open_positions() -> Tuple[bool, Dict[str, Any]]:
    """Forward declaration - will be implemented later"""
    pass

async def get_current_price(symbol: str, position_type: str = None) -> float:
    """Forward declaration - will be implemented later"""
    pass

async def get_atr(symbol: str, timeframe: str) -> float:
    """Forward declaration - will be implemented later"""
    pass

def standardize_symbol(symbol: str) -> str:
    """Forward declaration - will be implemented later"""
    pass

async def execute_trade_close(alert: AlertData) -> Tuple[bool, Dict[str, Any]]:
    """Forward declaration - will be implemented later"""
    pass

async def execute_trade_partial_close(alert: AlertData, percentage: float) -> Tuple[bool, Dict[str, Any]]:
    """Forward declaration - will be implemented later"""
    pass

def get_instrument_type(symbol: str) -> str:
    """Forward declaration - will be implemented later"""
    pass
    
class PositionManager:
    """Manager for tracking and analyzing trading positions"""
    
    def __init__(self):
        self._positions = {}  # Symbol -> Position data
        self._lock = asyncio.Lock()
        self._running = False
        self._initialized = False
        self._price_monitor_task = None
        self._reconciliation_task = None
        self._daily_pnl = 0.0
        self._pnl_reset_date = datetime.now().date()
        self._peak_balance = 0.0
        self._current_balance = 0.0
        
        # Market analysis components
        self._regime_data = {}  # Symbol -> Market regime data
        self._bar_times = {}  # Symbol -> List of bar timestamps
        
        # Risk tracking
        self._position_risk_data = {}  # Symbol -> Risk parameters
        self._correlation_matrix = {}  # Symbol -> Dict of correlations with other symbols
        self._portfolio_heat = 0.0  # Current portfolio heat
        
        # Analytics
        self._var_values = {}  # Symbol -> Value at Risk
        
        # Initialize market analysis classes
        self.volatility_monitor = VolatilityMonitor()
        self.regime_classifier = LorentzianDistanceClassifier()

    async def start(self):
        """Initialize and start the position manager"""
        if not self._initialized:
            async with self._lock:
                if not self._initialized:  # Double-check pattern
                    self._running = True
                    self._reconciliation_task = asyncio.create_task(self._reconcile_positions())
                    self._price_monitor_task = asyncio.create_task(self._monitor_prices())
                    self._initialized = True
                    logger.info("Position manager started")
    
    async def stop(self):
        """Gracefully stop the position manager"""
        self._running = False
        tasks = []
        
        if hasattr(self, '_reconciliation_task') and self._reconciliation_task:
            self._reconciliation_task.cancel()
            tasks.append(self._reconciliation_task)
            
        if hasattr(self, '_price_monitor_task') and self._price_monitor_task:
            self._price_monitor_task.cancel()
            tasks.append(self._price_monitor_task)
            
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("Position manager stopped")

    @handle_async_errors
    async def _reconcile_positions(self):
        """Reconcile positions with broker to ensure consistency"""
        while self._running:
            try:
                # Wait between reconciliation attempts
                await asyncio.sleep(900)  # Every 15 minutes
                
                logger.info("Starting position reconciliation")
                async with self._lock:
                    async with asyncio.timeout(60):  # 60 second timeout
                        success, positions_data = await get_open_positions()
                    
                        if not success:
                            logger.error("Failed to fetch positions for reconciliation")
                            continue
                    
                        # Convert broker positions to a set for lookup
                        broker_positions = {
                            standardize_symbol(p['instrument']) for p in positions_data.get('positions', [])
                        }
                    
                        # Check each tracked position
                        for symbol in list(self._positions.keys()):
                            try:
                                if standardize_symbol(symbol) not in broker_positions:
                                    # Position closed externally
                                    old_data = self._positions.pop(symbol, None)
                                    self._bar_times.pop(symbol, None)
                                    logger.warning(
                                        f"Removing stale position for {symbol}. "
                                        f"Old data: {old_data}"
                                    )
                            except Exception as e:
                                logger.error(
                                    f"Error reconciling position for {symbol}: {str(e)}"
                                )
                        
                        # Add any positions from broker that we're not tracking
                        for pos in positions_data.get('positions', []):
                            broker_symbol = standardize_symbol(pos['instrument'])
                            if broker_symbol not in self._positions:
                                # Extract position details
                                long_units = float(pos.get('long', {}).get('units', 0))
                                short_units = float(pos.get('short', {}).get('units', 0))
                                position_type = 'LONG' if long_units > 0 else 'SHORT'
                                units = long_units if position_type == 'LONG' else abs(short_units)
                                avg_price = float(pos.get('long' if position_type == 'LONG' else 'short', {})
                                                  .get('averagePrice', 0))
                                
                                # Create basic position data
                                self._positions[broker_symbol] = {
                                    'entry_price': avg_price,
                                    'position_type': position_type,
                                    'units': units,
                                    'current_units': units,
                                    'entry_time': datetime.now(timezone('Asia/Bangkok')),
                                    'timeframe': 'UNKNOWN',  # External position, we don't know timeframe
                                    'last_update': datetime.now(),
                                    'bars_held': 0,
                                    'pnl': float(pos.get('unrealizedPL', 0))
                                }
                                
                                logger.info(f"Added externally created position for {broker_symbol}")
                        
                        logger.info(
                            f"Reconciliation complete. Active positions: "
                            f"{list(self._positions.keys())}"
                        )
                        
            except asyncio.TimeoutError:
                logger.error("Position reconciliation timed out, will retry in next cycle")
                continue
            except asyncio.CancelledError:
                logger.info("Position reconciliation task cancelled")
                break
            except Exception as e:
                logger.error(f"Error in reconciliation loop: {str(e)}")
                await asyncio.sleep(60)  # Wait before retrying on unexpected errors

    async def _monitor_prices(self):
        """Monitor prices and update position statistics"""
        while self._running:
            try:
                positions = list(self._positions.keys())
                
                for symbol in positions:
                    try:
                        position = self._positions.get(symbol)
                        if not position:
                            continue
                            
                        # Get current price
                        current_price = await get_current_price(symbol, position['position_type'])
                        
                        # Update position data
                        async with self._lock:
                            # Skip if position no longer exists
                            if symbol not in self._positions:
                                continue
                                
                            # Update price and calculate P&L
                            position['current_price'] = current_price
                            
                            # Calculate unrealized P&L
                            if position['position_type'] == 'LONG':
                                unrealized_pnl = (current_price - position['entry_price']) * position['current_units']
                            else:
                                unrealized_pnl = (position['entry_price'] - current_price) * position['current_units']
                                
                            position['unrealized_pnl'] = unrealized_pnl
                            position['last_update'] = datetime.now()
                            
                            # Update market regime data
                            await self._update_market_regime(symbol, current_price)
                            
                            # Update risk analytics
                            await self._update_risk_metrics(symbol, current_price)
                            
                            # Check for exit conditions
                            await self._check_exit_conditions(symbol, current_price)
                            
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.error(f"Error monitoring position {symbol}: {str(e)}")
                        continue
                    
                # Sleep before next check
                await asyncio.sleep(15)  # Check every 15 seconds
                
            except asyncio.CancelledError:
                logger.info("Price monitoring cancelled")
                break
            except Exception as e:
                logger.error(f"Error in price monitoring: {str(e)}")
                await asyncio.sleep(60)  # Wait before retrying on error

    async def _update_market_regime(self, symbol: str, current_price: float):
        """Update market regime classification for a symbol using the extracted classes"""
        if symbol not in self._regime_data:
            self._regime_data[symbol] = {
                'price_history': [],
                'current_regime': 'UNKNOWN'
            }
            
        # Update price history
        regime_data = self._regime_data[symbol]
        regime_data['price_history'].append(current_price)
        
        # Keep last 30 prices
        if len(regime_data['price_history']) > 30:
            regime_data['price_history'].pop(0)
            
        # Need at least 5 prices for calculation
        if len(regime_data['price_history']) < 5:
            return
            
        # Get position timeframe
        position = self._positions.get(symbol, {})
        timeframe = position.get('timeframe', '1H')
        
        # Update volatility using VolatilityMonitor
        self._regime_data[symbol] = self.volatility_monitor.update_volatility(
            symbol, current_price, timeframe, regime_data
        )
        
        # Classify market regime using LorentzianDistanceClassifier
        new_regime = self.regime_classifier.classify_market_regime(
            current_price, regime_data
        )
        
        # Update regime history
        if 'regime_history' not in regime_data:
            regime_data['regime_history'] = []
            
        regime_data['regime_history'].append(new_regime)
        regime_data['current_regime'] = new_regime
        
        # Keep last 20 regime values
        if len(regime_data['regime_history']) > 20:
            regime_data['regime_history'].pop(0)
            
        # Apply regime-based adjustments to risk parameters if needed
        risk_data = self._position_risk_data.get(symbol)
        if risk_data:
            # Check if we should adjust for volatility
            should_adjust, vol_factor = self.volatility_monitor.should_adjust_risk(regime_data)
            
            # Get regime-based adjustment factors
            regime_factors = self.regime_classifier.get_regime_adjustment_factors(new_regime)
            
            # Combine adjustments if needed
            if should_adjust:
                for key in regime_factors:
                    regime_factors[key] *= vol_factor
                    
            # Store the adjustment factors for later use
            risk_data['adjustment_factors'] = regime_factors

    async def _update_risk_metrics(self, symbol: str, current_price: float):
        """Update risk metrics for position"""
        position = self._positions.get(symbol)
        if not position:
            return
            
        # Initialize risk data if not present
        if symbol not in self._position_risk_data:
            self._position_risk_data[symbol] = {
                'stop_loss': None,
                'take_profits': [],
                'trailing_stop': None,
                'risk_reward_ratio': 0.0,
                'time_in_position': 0,
                'var_95': 0.0,
                'es_95': 0.0
            }
            
        risk_data = self._position_risk_data[symbol]
        
        # Calculate time in position
        entry_time = position['entry_time']
        now = datetime.now(timezone('Asia/Bangkok'))
        time_in_position = (now - entry_time).total_seconds() / 3600  # Hours
        risk_data['time_in_position'] = time_in_position
        
        # Calculate risk/reward if stop_loss and take_profits are set
        if risk_data['stop_loss'] and risk_data['take_profits']:
            entry_price = position['entry_price']
            stop_loss = risk_data['stop_loss']
            take_profit_1 = risk_data['take_profits'][0] if risk_data['take_profits'] else None
            
            if position['position_type'] == 'LONG':
                risk = entry_price - stop_loss
                reward = take_profit_1 - entry_price if take_profit_1 else 0
            else:
                risk = stop_loss - entry_price
                reward = entry_price - take_profit_1 if take_profit_1 else 0
                
            risk_data['risk_reward_ratio'] = reward / risk if risk != 0 else 0
            
        # Update VaR and Expected Shortfall
        if symbol not in self._var_values:
            self._var_values[symbol] = {
                'returns': [],
                'var_95': 0.0,
                'es_95': 0.0
            }
            
        # Calculate return for this update
        var_data = self._var_values[symbol]
        if 'last_price' in var_data:
            latest_return = (current_price / var_data['last_price']) - 1
            var_data['returns'].append(latest_return)
            
            # Keep last 100 returns
            if len(var_data['returns']) > 100:
                var_data['returns'].pop(0)
                
            # Calculate VaR and ES if we have enough data
            if len(var_data['returns']) >= 20:
                var_95 = np.percentile(var_data['returns'], 5)  # 5th percentile
                es_95 = np.mean([r for r in var_data['returns'] if r <= var_95])
                
                var_data['var_95'] = var_95
                var_data['es_95'] = es_95
                
                # Update position risk data
                risk_data['var_95'] = var_95
                risk_data['es_95'] = es_95
                
        # Update last price
        var_data['last_price'] = current_price
        
    async def _check_exit_conditions(self, symbol: str, current_price: float):
        """Check if any exit conditions are met"""
        position = self._positions.get(symbol)
        if not position:
            return
            
        risk_data = self._position_risk_data.get(symbol, {})
        if not risk_data:
            return
            
        # Check stop loss
        if risk_data.get('stop_loss'):
            stop_loss = risk_data['stop_loss']
            if (position['position_type'] == 'LONG' and current_price <= stop_loss) or \
               (position['position_type'] == 'SHORT' and current_price >= stop_loss):
                # Stop loss hit
                logger.info(f"Stop loss hit for {symbol} at {current_price}")
                
                # Close position in a separate task to avoid blocking
                asyncio.create_task(self.close_position(symbol, 'Stop loss hit'))
                return
                
        # Check take profits
        take_profits = risk_data.get('take_profits', [])
        for i, tp in enumerate(take_profits):
            if (position['position_type'] == 'LONG' and current_price >= tp) or \
               (position['position_type'] == 'SHORT' and current_price <= tp):
                # Take profit hit
                logger.info(f"Take profit {i+1} hit for {symbol} at {current_price}")
                
                # Close % of position based on take profit level
                tp_size = TIMEFRAME_TAKE_PROFIT_LEVELS.get(
                    position.get('timeframe', '1H'), 
                    TIMEFRAME_TAKE_PROFIT_LEVELS['1H']
                )
                
                if i == 0:  # First take profit
                    exit_percentage = tp_size['first_exit'] * 100
                elif i == 1:  # Second take profit
                    exit_percentage = tp_size['second_exit'] * 100
                else:  # Runner
                    exit_percentage = tp_size['runner'] * 100
                    
                # Close partial position
                asyncio.create_task(self.close_partial_position(
                    symbol, exit_percentage, f'Take profit {i+1} hit'
                ))
                
                # Remove this take profit level
                risk_data['take_profits'].pop(i)
                return
                
        # Check trailing stop
        if risk_data.get('trailing_stop'):
            trailing_stop = risk_data['trailing_stop']
            if (position['position_type'] == 'LONG' and current_price <= trailing_stop) or \
               (position['position_type'] == 'SHORT' and current_price >= trailing_stop):
                # Trailing stop hit
                logger.info(f"Trailing stop hit for {symbol} at {current_price}")
                
                # Close position
                asyncio.create_task(self.close_position(symbol, 'Trailing stop hit'))
                return
                
        # Check time-based exits
        if position.get('timeframe') in TIMEFRAME_TIME_STOPS:
            time_stop = TIMEFRAME_TIME_STOPS[position['timeframe']]
            time_in_position = risk_data.get('time_in_position', 0)
            
            if time_in_position > time_stop['max_duration']:
                # Maximum duration exceeded
                logger.info(f"Maximum time in position exceeded for {symbol}: {time_in_position} hours")
                
                # Close position
                asyncio.create_task(self.close_position(symbol, 'Time stop hit'))
                return
                
            elif time_in_position > time_stop['optimal_duration']:
                # Past optimal duration, tighten stop loss
                if risk_data.get('stop_loss') and not risk_data.get('stop_adjusted_for_time', False):
                    entry_price = position['entry_price']
                    current_stop = risk_data['stop_loss']
                    
                    # Calculate adjusted stop (tighter)
                    adjustment = time_stop['stop_adjustment']
                    if position['position_type'] == 'LONG':
                        new_stop = entry_price - (entry_price - current_stop) * adjustment
                    else:
                        new_stop = entry_price + (current_stop - entry_price) * adjustment
                        
                    # Update stop loss
                    risk_data['stop_loss'] = new_stop
                    risk_data['stop_adjusted_for_time'] = True
                    
                    logger.info(f"Adjusted stop loss for {symbol} based on time in position")
    
    async def update_trailing_stop(self, symbol: str, current_price: float):
        """Update trailing stop based on current price and profit level"""
        position = self._positions.get(symbol)
        if not position:
            return
            
        risk_data = self._position_risk_data.get(symbol, {})
        if not risk_data:
            return
            
        # Get trailing settings based on timeframe
        timeframe = position.get('timeframe', '1H')
        trailing_settings = TIMEFRAME_TRAILING_SETTINGS.get(
            timeframe, TIMEFRAME_TRAILING_SETTINGS['1H']
        )
        
        # Calculate current R:R ratio
        entry_price = position['entry_price']
        stop_loss = risk_data.get('stop_loss')
        
        if not stop_loss:
            return
            
        # Calculate risk in price terms
        risk = abs(entry_price - stop_loss)
        
        # Calculate current reward
        if position['position_type'] == 'LONG':
            current_reward = current_price - entry_price
        else:
            current_reward = entry_price - current_price
            
        # Calculate R:R
        rr_ratio = current_reward / risk if risk != 0 else 0
        
        # Determine appropriate trailing stop multiplier based on profit level
        multiplier = trailing_settings['initial_multiplier']
        
        # Check profit levels and adjust multiplier
        for level in trailing_settings['profit_levels']:
            if rr_ratio >= level['threshold']:
                multiplier = level['multiplier']
                
        # Calculate new potential trailing stop
        if position['position_type'] == 'LONG':
            new_stop = current_price - (risk * multiplier)
            
            # Only update if new stop is higher than current
            current_trailing_stop = risk_data.get('trailing_stop')
            if current_trailing_stop is None or new_stop > current_trailing_stop:
                risk_data['trailing_stop'] = new_stop
                logger.info(f"Updated trailing stop for {symbol} to {new_stop}")
        else:
            new_stop = current_price + (risk * multiplier)
            
            # Only update if new stop is lower than current
            current_trailing_stop = risk_data.get('trailing_stop')
            if current_trailing_stop is None or new_stop < current_trailing_stop:
                risk_data['trailing_stop'] = new_stop
                logger.info(f"Updated trailing stop for {symbol} to {new_stop}")

    @handle_async_errors
async def add_position(self, symbol: str, position_type: str, entry_price: float, 
                     units: float, timeframe: str, account_balance: float = 0.0,
                     stop_loss: float = None, take_profits: List[float] = None,
                     order_result: Dict[str, Any] = None):
    """Add a new position with comprehensive tracking"""
    
    # Check if the order was actually filled by examining the order_result
    if order_result and 'orderCancelTransaction' in order_result:
        cancel_reason = order_result.get('orderCancelTransaction', {}).get('reason')
        if cancel_reason:
            logger.warning(f"Not adding position for {symbol} - order was canceled: {cancel_reason}")
            return False
    
    async with self._lock:
        current_time = datetime.now(timezone('Asia/Bangkok'))
        
        # Calculate default stop loss and take profits if not provided
        if stop_loss is None or take_profits is None:
            atr = await get_atr(symbol, timeframe)
            instrument_type = get_instrument_type(symbol)
            atr_multiplier = get_atr_multiplier(instrument_type, timeframe)
            
            if stop_loss is None:
                if position_type == 'LONG':
                    stop_loss = entry_price - (atr * atr_multiplier)
                else:
                    stop_loss = entry_price + (atr * atr_multiplier)
            
            if take_profits is None:
                if position_type == 'LONG':
                    take_profits = [
                        entry_price + (atr * atr_multiplier),  # 1:1
                        entry_price + (atr * atr_multiplier * 2),  # 2:1
                        entry_price + (atr * atr_multiplier * 3)  # 3:1
                    ]
                else:
                    take_profits = [
                        entry_price - (atr * atr_multiplier),  # 1:1
                        entry_price - (atr * atr_multiplier * 2),  # 2:1
                        entry_price - (atr * atr_multiplier * 3)  # 3:1
                    ]
        
        # Create position data
        position_data = {
            'entry_time': current_time,
            'entry_price': entry_price,
            'position_type': position_type,
            'units': units,
            'current_units': units,
            'timeframe': timeframe,
            'last_update': current_time,
            'bars_held': 0,
            'unrealized_pnl': 0.0,
            'current_price': entry_price
        }
        
        # Create risk data
        risk_data = {
            'stop_loss': stop_loss,
            'take_profits': take_profits,
            'trailing_stop': None,
            'risk_reward_ratio': 0.0,
            'time_in_position': 0,
            'var_95': 0.0,
            'es_95': 0.0,
            'stop_adjusted_for_time': False
        }
        
        # Save data
        self._positions[symbol] = position_data
        self._position_risk_data[symbol] = risk_data
        self._bar_times.setdefault(symbol, []).append(current_time)
        
        # Update portfolio heat
        if account_balance > 0:
            self._portfolio_heat += (units * entry_price) / account_balance
            
            # Also update peak balance if needed
            if account_balance > self._peak_balance:
                self._peak_balance = account_balance
            self._current_balance = account_balance
        
        logger.info(f"Added position for {symbol}: {position_data}")
        logger.info(f"Risk parameters for {symbol}: Stop: {stop_loss}, TPs: {take_profits}")
        
        return True
            
    @handle_async_errors
    async def update_position(self, symbol: str, updates: Dict[str, Any]):
        """Update position parameters"""
        async with self._lock:
            position = self._positions.get(symbol)
            if not position:
                return False
                
            # Apply updates to position
            for key, value in updates.items():
                if key in position:
                    position[key] = value
                    
            # Update last update time
            position['last_update'] = datetime.now(timezone('Asia/Bangkok'))
            
            logger.info(f"Updated position for {symbol}: {updates}")
            return True
            
    @handle_async_errors
    async def update_risk_parameters(self, symbol: str, updates: Dict[str, Any]):
        """Update risk parameters for a position"""
        async with self._lock:
            risk_data = self._position_risk_data.get(symbol)
            if not risk_data:
                return False
                
            # Apply updates to risk data
            for key, value in updates.items():
                if key in risk_data:
                    risk_data[key] = value
                    
            logger.info(f"Updated risk parameters for {symbol}: {updates}")
            return True
            
    @handle_async_errors
    async def close_position(self, symbol: str, reason: str = "Manual close"):
        """Close a position completely"""
        async with self._lock:
            position = self._positions.get(symbol)
            if not position:
                logger.warning(f"Cannot close position for {symbol} - not found")
                return False
                
            # Create close alert
            close_alert = {
                'symbol': symbol,
                'action': 'CLOSE',
                'timeframe': position.get('timeframe', '1H'),
                'account': config.oanda_account
            }
            
            logger.info(f"Closing position for {symbol} - Reason: {reason}")
            
            # Remove position from tracking before API call
            # This prevents race conditions if the API is slow
            position_copy = self._positions.pop(symbol, None)
            risk_data = self._position_risk_data.pop(symbol, None)
            bar_times = self._bar_times.pop(symbol, None)
            
            # Remove from regime data
            self._regime_data.pop(symbol, None)
            
            # Remove from VaR data
            self._var_values.pop(symbol, None)
            
            # Execute the close with the broker
            success, result = await execute_trade_close(close_alert)
            
            if success:
                # Calculate P&L
                pnl = 0.0
                try:
                    # Extract P&L from transaction details
                    if 'longOrderFillTransaction' in result and result['longOrderFillTransaction']:
                        pnl += float(result['longOrderFillTransaction'].get('pl', 0))
                    
                    if 'shortOrderFillTransaction' in result and result['shortOrderFillTransaction']:
                        pnl += float(result['shortOrderFillTransaction'].get('pl', 0))
                    
                    # Update daily P&L
                    await self.record_pnl(pnl)
                    
                    logger.info(f"Position closed for {symbol} with P&L: {pnl}")
                except Exception as e:
                    logger.error(f"Error calculating P&L for {symbol}: {str(e)}")
                
                return True
            else:
                # If close failed, restore position data
                if position_copy:
                    self._positions[symbol] = position_copy
                if risk_data:
                    self._position_risk_data[symbol] = risk_data
                if bar_times:
                    self._bar_times[symbol] = bar_times
                    
                logger.error(f"Failed to close position for {symbol}: {result.get('error', 'Unknown error')}")
                return False
                
    @handle_async_errors
    async def close_partial_position(self, symbol: str, percentage: float, reason: str = "Partial take profit"):
        """Close a percentage of a position"""
        async with self._lock:
            position = self._positions.get(symbol)
            if not position:
                logger.warning(f"Cannot close partial position for {symbol} - not found")
                return False
                
            # Check if percentage is valid
            if percentage <= 0 or percentage >= 100:
                logger.error(f"Invalid percentage for partial close: {percentage}")
                return False
                
            # Calculate units to close
            current_units = position['current_units']
            units_to_close = current_units * (percentage / 100.0)
            
            # Create close alert
            close_alert = AlertData(
                symbol=symbol,
                action='CLOSE',
                timeframe=position.get('timeframe', '1H'),
                account=config.oanda_account,
                percentage=percentage
            )
            
            logger.info(f"Closing {percentage}% of position for {symbol} - Reason: {reason}")
            
            # Execute the partial close with the broker
            success, result = await execute_trade_partial_close(close_alert, percentage)
            
            if success:
                # Update position units
                position['current_units'] = current_units - units_to_close
                
                # Calculate P&L
                pnl = 0.0
                try:
                    # Extract P&L from transaction details
                    if 'longOrderFillTransaction' in result and result['longOrderFillTransaction']:
                        pnl += float(result['longOrderFillTransaction'].get('pl', 0))
                    
                    if 'shortOrderFillTransaction' in result and result['shortOrderFillTransaction']:
                        pnl += float(result['shortOrderFillTransaction'].get('pl', 0))
                    
                    # Update daily P&L
                    await self.record_pnl(pnl)
                    
                    logger.info(f"Partial position close for {symbol} with P&L: {pnl}")
                except Exception as e:
                    logger.error(f"Error calculating P&L for partial close of {symbol}: {str(e)}")
                
                return True
            else:
                logger.error(f"Failed to partially close position for {symbol}: {result.get('error', 'Unknown error')}")
                return False

    @handle_async_errors
    async def record_pnl(self, pnl: float):
        """Record P&L and reset daily if needed"""
        async with self._lock:
            current_date = datetime.now().date()
            
            # Reset daily P&L if it's a new day
            if current_date != self._pnl_reset_date:
                logger.info(f"Resetting daily P&L (was {self._daily_pnl}) for new day: {current_date}")
                self._daily_pnl = 0.0
                self._pnl_reset_date = current_date
            
            # Add the P&L to today's total
            self._daily_pnl += pnl
            
            # Update current balance
            self._current_balance += pnl
            
            # Update peak balance if needed
            if self._current_balance > self._peak_balance:
                self._peak_balance = self._current_balance
                
            logger.info(f"Updated daily P&L: {self._daily_pnl}")
            
    async def get_daily_pnl(self) -> float:
        """Get current daily P&L with automatic reset"""
        async with self._lock:
            # Reset if it's a new day
            current_date = datetime.now().date()
            if current_date != self._pnl_reset_date:
                self._daily_pnl = 0.0
                self._pnl_reset_date = current_date
            
            return self._daily_pnl
            
    async def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get comprehensive position information including risk data"""
        async with self._lock:
            position = self._positions.get(symbol)
            if not position:
                return None
                
            # Combine with risk data
            risk_data = self._position_risk_data.get(symbol, {})
            regime_data = self._regime_data.get(symbol, {})
            
            # Create full position info
            full_position = {
                **position,
                'risk': risk_data,
                'market_regime': regime_data.get('current_regime', 'UNKNOWN')
            }
            
            return full_position
            
    async def get_all_positions(self) -> Dict[str, Dict[str, Any]]:
        """Get all positions with comprehensive data"""
        async with self._lock:
            result = {}
            
            for symbol, position in self._positions.items():
                # Combine with risk data
                risk_data = self._position_risk_data.get(symbol, {})
                regime_data = self._regime_data.get(symbol, {})
                
                # Create full position info
                full_position = {
                    **position,
                    'risk': risk_data,
                    'market_regime': regime_data.get('current_regime', 'UNKNOWN')
                }
                
                result[symbol] = full_position
                
            return result
            
    async def check_max_daily_loss(self, account_balance: float) -> Tuple[bool, float]:
        """Check if maximum daily loss has been exceeded"""
        daily_pnl = await self.get_daily_pnl()
        
        # Calculate only losses (negative values)
        daily_loss = min(0, daily_pnl)
        loss_percentage = abs(daily_loss) / account_balance if account_balance > 0 else 0
        
        # Check against maximum allowed loss
        max_loss_exceeded = loss_percentage >= MAX_DAILY_LOSS
        
        if loss_percentage > MAX_DAILY_LOSS * 0.75:  # Warning at 75% of limit
            logger.warning(f"Daily loss at {loss_percentage:.2%} of account (limit: {MAX_DAILY_LOSS:.2%})")
            
        if max_loss_exceeded:
            logger.error(f"MAXIMUM DAILY LOSS EXCEEDED: {loss_percentage:.2%} > {MAX_DAILY_LOSS:.2%}")
            
        return max_loss_exceeded, loss_percentage
        
    async def calculate_portfolio_metrics(self, account_balance: float) -> Dict[str, Any]:
        """Calculate comprehensive portfolio risk metrics"""
        async with self._lock:
            # Get all positions
            positions = self._positions.copy()
            
            # Calculate total value and risk exposure
            total_value = 0.0
            total_var = 0.0
            position_count = len(positions)
            
            for symbol, position in positions.items():
                price = position.get('current_price', position['entry_price'])
                units = position['current_units']
                position_value = price * units
                total_value += position_value
                
                # Add VaR if available
                var_data = self._var_values.get(symbol, {})
                var_95 = var_data.get('var_95', 0.0)
                position_var = position_value * abs(var_95) if var_95 != 0 else 0
                total_var += position_var
                
            # Calculate exposure as % of account
            exposure_percentage = (total_value / account_balance) if account_balance > 0 else 0
            
            # Get daily PnL metrics
            daily_pnl = self._daily_pnl
            daily_pnl_percentage = (daily_pnl / account_balance) if account_balance > 0 else 0
            
            # Calculate drawdown
            drawdown = (self._peak_balance - self._current_balance) / self._peak_balance if self._peak_balance > 0 else 0
            
            return {
                'total_positions': position_count,
                'total_exposure': total_value,
                'exposure_percentage': exposure_percentage,
                'total_var': total_var,
                'var_percentage': (total_var / account_balance) if account_balance > 0 else 0,
                'daily_pnl': daily_pnl,
                'daily_pnl_percentage': daily_pnl_percentage,
                'drawdown': drawdown,
                'portfolio_heat': self._portfolio_heat
            }
            
    async def get_position_correlation_matrix(self) -> Dict[str, Dict[str, float]]:
        """Calculate correlation between all active positions"""
        async with self._lock:
            # Need at least 2 positions
            if len(self._positions) < 2:
                return {}
                
            symbols = list(self._positions.keys())
            result = {}
            
            for i, symbol1 in enumerate(symbols):
                result[symbol1] = {}
                
                for j, symbol2 in enumerate(symbols):
                    if i == j:  # Same symbol
                        result[symbol1][symbol2] = 1.0
                        continue
                        
                    # Calculate correlation
                    correlation = await self._calculate_correlation(symbol1, symbol2)
                    result[symbol1][symbol2] = correlation
                    
            return result
            
    async def _calculate_correlation(self, symbol1: str, symbol2: str) -> float:
        """Calculate correlation between two symbols"""
        pos1 = self._positions.get(symbol1)
        pos2 = self._positions.get(symbol2)
        
        if not pos1 or not pos2:
            return 0.0
            
        # Check for common currencies in the pairs
        base1, quote1 = symbol1.split('_') if '_' in symbol1 else (symbol1, 'USD')
        base2, quote2 = symbol2.split('_') if '_' in symbol2 else (symbol2, 'USD')
        
        # Position types
        type1 = pos1['position_type']
        type2 = pos2['position_type']
        
        # Check for currency overlap
        if base1 == base2 or quote1 == quote2:
            # Same base or quote currency indicates correlation
            if type1 == type2:
                return 0.8  # Strong positive correlation
            else:
                return -0.8  # Strong negative correlation
                
        # Check for cross-currency correlation
        if base1 == quote2 or quote1 == base2:
            if type1 == type2:
                return 0.6  # Moderate positive correlation
            else:
                return -0.6  # Moderate negative correlation
                
        # For other pairs, use a default correlation
        # In a real system, this would use historical correlation data
        return 0.3  # Assumed slight correlation for diversified currencies
        
# API implementation
app = FastAPI(title="Trading Bot API", version="1.0.0")

# Initialize position manager
position_manager = PositionManager()

async def startup_event():
    """Initialize resources on startup"""
    await init_db()
    await position_manager.start()

async def startup_event():
    """Initialize resources on startup"""
    await init_db()
    await position_manager.start()
    asyncio.create_task(cleanup_sessions())

# Health check endpoint
@app.get("/health", status_code=200)
async def health_check():
    """Health check endpoint"""
    return {"status": "ok", "version": "1.0.0"}

# Account information endpoint
@app.get("/account", response_model=Dict[str, Any])
async def get_account_info():
    """Get account information"""
    success, account_info = await get_account_details()
    
    if success:
        # Get portfolio metrics
        account_balance = float(account_info.get('account', {}).get('balance', 0))
        metrics = await position_manager.calculate_portfolio_metrics(account_balance)
        
        # Add to account info
        account_info['portfolio_metrics'] = metrics
        
        return account_info
    else:
        raise HTTPException(status_code=500, detail="Failed to fetch account information")

# Positions endpoints
@app.get("/positions", response_model=Dict[str, Any])
async def get_positions():
    """Get all tracked positions"""
    positions = await position_manager.get_all_positions()
    return {"positions": positions}

@app.get("/positions/{symbol}", response_model=Dict[str, Any])
async def get_position(symbol: str):
    """Get position details for a specific symbol"""
    position = await position_manager.get_position(symbol)
    
    if position:
        return {"position": position}
    else:
        raise HTTPException(status_code=404, detail=f"Position for {symbol} not found")

@app.delete("/positions/{symbol}", response_model=Dict[str, Any])
async def close_position(symbol: str):
    """Close a position"""
    success = await position_manager.close_position(symbol, "Manual API close")
    
    if success:
        return {"status": "success", "message": f"Position for {symbol} closed"}
    else:
        raise HTTPException(status_code=500, detail=f"Failed to close position for {symbol}")

# Alert processing endpoint
@app.post("/alert", response_model=Dict[str, Any])
async def process_alert(alert: AlertData):
    """Process incoming trading alert"""
    try:
        # Change this line in the process_alert function
        logger.info(f"Received alert: {alert.model_dump()}")
        
        # Check if market is open
        if not await is_market_open(alert.symbol):
            return {"status": "rejected", "reason": "Market closed"}
            
        # Check if instrument is tradeable
        if not await is_tradeable(alert.symbol):
            return {"status": "rejected", "reason": "Instrument not tradeable"}
        
        # Get account info
        success, account_info = await get_account_details()
        if not success:
            return {"status": "error", "reason": "Failed to get account details"}
            
        account_balance = float(account_info.get('account', {}).get('balance', 0))
        
        # Check daily loss limit
        max_loss_exceeded, loss_percentage = await position_manager.check_max_daily_loss(account_balance)
        if max_loss_exceeded:
            return {
                "status": "rejected", 
                "reason": f"Maximum daily loss exceeded: {loss_percentage:.2%}"
            }
        
        # Process according to action type
        if alert.action in ['BUY', 'SELL']:
            # Execute trade
            success, result = await execute_trade(alert)
            
            if success:
                # Get order details
                order_price = float(result.get('orderFillTransaction', {}).get('price', 0))
                order_units = float(result.get('orderFillTransaction', {}).get('units', 0))
                
                # Add position to manager
                position_type = 'LONG' if alert.action == 'BUY' else 'SHORT'
                
                # Extract stop loss and take profit from result
                order_data = result.get('orderCreateTransaction', {})
                stop_loss = None
                take_profits = None
                
                if 'stopLossOnFill' in order_data:
                    stop_loss = float(order_data['stopLossOnFill'].get('price', 0))
                
                if 'takeProfitOnFill' in order_data:
                    tp1 = float(order_data['takeProfitOnFill'].get('price', 0))
                    
                    # Calculate additional take profits based on the first one
                    if position_type == 'LONG':
                        distance = tp1 - order_price
                        tp2 = tp1 + distance
                        tp3 = tp2 + distance
                    else:
                        distance = order_price - tp1
                        tp2 = tp1 - distance
                        tp3 = tp2 - distance
                        
                    take_profits = [tp1, tp2, tp3]
                
                await position_manager.add_position(
                    symbol=alert.symbol,
                    position_type=position_type,
                    entry_price=order_price,
                    units=abs(order_units),
                    timeframe=alert.timeframe,
                    account_balance=account_balance,
                    stop_loss=stop_loss,
                    take_profits=take_profits
                )
                
                return {
                    "status": "success", 
                    "order": result,
                    "message": f"{alert.action} order executed for {alert.symbol}"
                }
            else:
                return {"status": "error", "reason": f"Trade execution failed: {result}"}
                
        elif alert.action == 'CLOSE':
            # Check for partial close
            if alert.percentage and alert.percentage > 0 and alert.percentage < 100:
                success = await position_manager.close_partial_position(
                    alert.symbol, alert.percentage, "API partial close"
                )
            else:
                success = await position_manager.close_position(alert.symbol, "API close")
                
            if success:
                return {
                    "status": "success",
                    "message": f"Position for {alert.symbol} {'partially ' if alert.percentage else ''}closed"
                }
            else:
                return {"status": "error", "reason": f"Failed to close position for {alert.symbol}"}
                
        elif alert.action == 'UPDATE':
            # Update stop loss / take profit
            if not alert.position_update:
                return {"status": "error", "reason": "No position update data provided"}
                
            success = await position_manager.update_risk_parameters(
                alert.symbol, alert.position_update
            )
            
            if success:
                return {
                    "status": "success",
                    "message": f"Risk parameters updated for {alert.symbol}"
                }
            else:
                return {"status": "error", "reason": f"Failed to update position for {alert.symbol}"}
        else:
            return {"status": "error", "reason": f"Unknown action: {alert.action}"}
            
    except Exception as e:
        logger.error(f"Error processing alert: {str(e)}")
        return {"status": "error", "reason": str(e)}

# Analytics endpoints
@app.get("/analytics/risk", response_model=Dict[str, Any])
async def get_risk_analytics():
    """Get risk analytics for all positions"""
    try:
        # Get account info
        success, account_info = await get_account_details()
        if not success:
            raise HTTPException(status_code=500, detail="Failed to get account details")
            
        account_balance = float(account_info.get('account', {}).get('balance', 0))
        
        # Calculate risk metrics
        metrics = await position_manager.calculate_portfolio_metrics(account_balance)
        
        # Add correlation matrix
        correlation_matrix = await position_manager.get_position_correlation_matrix()
        
        return {
            "account_balance": account_balance,
            "metrics": metrics,
            "correlation_matrix": correlation_matrix
        }
    except Exception as e:
        logger.error(f"Error getting risk analytics: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# OANDA API Interaction
async def get_account_details() -> Tuple[bool, Dict[str, Any]]:
    """Get account details from OANDA"""
    url = f"{config.oanda_api_url}/v3/accounts/{config.oanda_account}"
    
    try:
        session = await get_session()
        headers = {
            "Authorization": f"Bearer {config.oanda_api_key}",
            "Content-Type": "application/json"
        }
        
        async with asyncio.timeout(10):
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    return True, data
                else:
                    error_text = await response.text()
                    logger.error(f"Error fetching account details: {error_text}")
                    return False, {"error": error_text}
    except Exception as e:
        logger.error(f"Exception fetching account details: {str(e)}")
        return False, {"error": str(e)}

async def get_open_positions() -> Tuple[bool, Dict[str, Any]]:
    """Get open positions from OANDA"""
    url = f"{config.oanda_api_url}/v3/accounts/{config.oanda_account}/openPositions"
    
    try:
        session = await get_session()
        headers = {
            "Authorization": f"Bearer {config.oanda_api_key}",
            "Content-Type": "application/json"
        }
        
        async with asyncio.timeout(10):
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    return True, data
                else:
                    error_text = await response.text()
                    logger.error(f"Error fetching open positions: {error_text}")
                    return False, {"error": error_text}
    except Exception as e:
        logger.error(f"Exception fetching open positions: {str(e)}")
        return False, {"error": str(e)}

async def get_current_price(symbol: str, position_type: str = None) -> float:
    """Get current price for a symbol"""
    url = f"{config.oanda_api_url}/v3/accounts/{config.oanda_account}/pricing"
    
    try:
        session = await get_session()
        headers = {
            "Authorization": f"Bearer {config.oanda_api_key}",
            "Content-Type": "application/json"
        }
        
        # Replace _ with / for OANDA format
        oanda_symbol = symbol.replace('_', '_')
        
        params = {
            "instruments": oanda_symbol
        }
        
        async with asyncio.timeout(5):
            async with session.get(url, headers=headers, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    if 'prices' in data and len(data['prices']) > 0:
                        price_data = data['prices'][0]
                        
                        if position_type == 'LONG':
                            # For long positions, use ask price (the price you can sell at)
                            return float(price_data['closeoutAsk'])
                        elif position_type == 'SHORT':
                            # For short positions, use bid price (the price you can buy at)
                            return float(price_data['closeoutBid'])
                        else:
                            # If no position type specified, use mid price
                            ask = float(price_data['closeoutAsk'])
                            bid = float(price_data['closeoutBid'])
                            return (ask + bid) / 2
                    else:
                        raise ValueError(f"No price data available for {symbol}")
                else:
                    error_text = await response.text()
                    logger.error(f"Error fetching price for {symbol}: {error_text}")
                    raise ValueError(f"Failed to get price: {error_text}")
    except Exception as e:
        logger.error(f"Exception fetching price for {symbol}: {str(e)}")
        raise ValueError(f"Price fetch exception: {str(e)}")

async def get_atr(symbol: str, timeframe: str) -> float:
    """Get Average True Range for a symbol and timeframe"""
    try:
        # In a full implementation, this would call the broker API to get historical data
        # and calculate ATR. For now, we'll use a simplified approach with defaults.
        
        # Default ATR values based on symbol type and timeframe
        defaults = {
            'FOREX': {
                '1m': 0.0002,
                '5m': 0.0005,
                '15m': 0.0008,
                '30m': 0.0012,
                '1H': 0.0020,
                '4H': 0.0040,
                'D': 0.0080
            },
            'FOREX_JPY': {
                '1m': 0.02,
                '5m': 0.05,
                '15m': 0.08,
                '30m': 0.12,
                '1H': 0.20,
                '4H': 0.40,
                'D': 0.80
            },
            'METAL': {
                '1m': 0.10,
                '5m': 0.25,
                '15m': 0.40,
                '30m': 0.60,
                '1H': 1.00,
                '4H': 2.00,
                'D': 4.00
            },
            'INDEX': {
                '1m': 1.0,
                '5m': 2.5,
                '15m': 4.0,
                '30m': 6.0,
                '1H': 10.0,
                '4H': 20.0,
                'D': 40.0
            },
            'ENERGY': {
                '1m': 0.05,
                '5m': 0.10,
                '15m': 0.15,
                '30m': 0.25,
                '1H': 0.50,
                '4H': 1.00,
                'D': 2.00
            }
        }
        
        # Get instrument type
        instrument_type = get_instrument_type(symbol)
        
        # Get ATR for this type and timeframe, default to FOREX 1H if not found
        type_defaults = defaults.get(instrument_type, defaults['FOREX'])
        atr = type_defaults.get(timeframe, type_defaults['1H'])
        
        return atr
        
    except Exception as e:
        logger.error(f"Error getting ATR for {symbol}: {str(e)}")
        # Return a reasonable default
        return 0.001  # Default 10 pips for FOREX

def get_instrument_type(symbol: str) -> str:
    """Determine the type of instrument from the symbol"""
    symbol = symbol.upper()
    
    # FOREX pairs
    if '_' in symbol or '/' in symbol:
        parts = symbol.replace('/', '_').split('_')
        if len(parts) == 2:
            # Check if it's a JPY pair
            if 'JPY' in parts:
                return 'FOREX_JPY'
            return 'FOREX'
    
    # METALS
    if symbol.startswith('XAU') or symbol.startswith('XAG') or symbol.startswith('GOLD'):
        return 'METAL'
        
    # INDICES
    indices = ['SPX', 'NAS', 'DOW', 'DAX', 'FTSE', 'NKY', 'US30', 'US500', 'USTEC']
    if any(idx in symbol for idx in indices):
        return 'INDEX'
        
    # ENERGY
    energy = ['OIL', 'BRENT', 'WTI', 'NAT', 'GAS']
    if any(e in symbol for e in energy):
        return 'ENERGY'
        
    # Default to FOREX if unknown
    return 'FOREX'

def get_atr_multiplier(instrument_type: str, timeframe: str) -> float:
    """Get appropriate ATR multiplier based on instrument type and timeframe"""
    # Base multipliers - these adjust the stop-loss distance as a multiple of ATR
    base_multipliers = {
        'FOREX': 1.5,
        'FOREX_JPY': 1.5,
        'METAL': 1.2,
        'INDEX': 1.0,
        'ENERGY': 1.2
    }
    
    # Timeframe adjustments - higher timeframes get slightly wider stops
    timeframe_adjustments = {
        '1m': 0.8,  # Tighter stops on lower timeframes
        '5m': 0.9,
        '15m': 1.0,
        '30m': 1.1,
        '1H': 1.2,
        '4H': 1.3,
        'D': 1.5    # Wider stops on daily chart
    }
    
    # Get base multiplier, default to FOREX if not found
    base = base_multipliers.get(instrument_type, base_multipliers['FOREX'])
    
    # Get timeframe adjustment, default to 1H if not found
    adjustment = timeframe_adjustments.get(timeframe, timeframe_adjustments['1H'])
    
    return base * adjustment

async def calculate_position_size(symbol: str, risk_percentage: float) -> int:
    """Calculate position size based on risk percentage"""
    try:
        # Get account info
        success, account_info = await get_account_details()
        if not success:
            logger.error("Failed to get account details for position sizing")
            return 1000  # Default to 1000 units
            
        # Get account balance
        account_balance = float(account_info.get('account', {}).get('balance', 0))
        if account_balance <= 0:
            logger.error("Invalid account balance for position sizing")
            return 1000  # Default to 1000 units
            
        # Apply risk percentage
        # Ensure risk_percentage is within limits
        risk_percentage = min(max(risk_percentage, 0.1), MAX_RISK_PERCENTAGE)
        
        # Calculate risk amount
        risk_amount = account_balance * (risk_percentage / 100.0)
        
        # Get symbol price
        price = await get_current_price(symbol)
        
        # Get ATR for stop loss estimation
        timeframe = '1H'  # Default to 1H if not specified
        atr = await get_atr(symbol, timeframe)
        instrument_type = get_instrument_type(symbol)
        atr_multiplier = get_atr_multiplier(instrument_type, timeframe)
        
        # Estimate stop loss distance in price terms
        stop_distance = atr * atr_multiplier
        
        # Calculate position size
        position_value = risk_amount / (stop_distance / price)
        
        # Apply instrument multiplier
        multiplier = INSTRUMENT_MULTIPLIERS.get(instrument_type, INSTRUMENT_MULTIPLIERS['FOREX'])
        
        # Calculate units
        units = int(position_value * multiplier)
        
        # Ensure minimum size
        min_size = 100 if instrument_type == 'FOREX' else 1
        units = max(units, min_size)
        
        logger.info(f"Calculated position size for {symbol}: {units} units")
        return units
        
    except Exception as e:
        logger.error(f"Error calculating position size: {str(e)}")
        return 1000  # Default to 1000 units

@handle_async_errors
async def execute_trade(alert: AlertData) -> Tuple[bool, Dict[str, Any]]:
    """Execute a trade based on alert data"""
    try:
        logger.info(f"Executing {alert.action} for {alert.symbol}")
        
        # Get position size
        risk_percentage = alert.risk_percentage or DEFAULT_RISK_PERCENTAGE
        position_size = await calculate_position_size(alert.symbol, risk_percentage)
        
        # Get current price
        price_type = "ask" if alert.action == "BUY" else "bid"
        
        # Prepare order data
        order_data = {
            "order": {
                "instrument": standardize_symbol(alert.symbol),
                "units": str(position_size) if alert.action == "BUY" else str(-position_size),
                "type": "MARKET",
                "positionFill": "DEFAULT",
                "timeInForce": "FOK"
            }
        }
        
        # Add stop loss and take profit based on ATR if possible
        atr = await get_atr(alert.symbol, alert.timeframe)
        if atr > 0:
            instrument_type = get_instrument_type(alert.symbol)
            atr_multiplier = get_atr_multiplier(instrument_type, alert.timeframe)
            
            # Get current price for calculation
            current_price = await get_current_price(alert.symbol)
            
            # Calculate stop loss and take profit
            if alert.action == "BUY":
                stop_loss = current_price - (atr * atr_multiplier)
                take_profit = current_price + (atr * atr_multiplier)
            else:
                stop_loss = current_price + (atr * atr_multiplier)
                take_profit = current_price - (atr * atr_multiplier)
                
            # Add to order
            order_data["order"]["stopLossOnFill"] = {
                "timeInForce": "GTC",
                "price": str(round(stop_loss, 5))
            }
            
            order_data["order"]["takeProfitOnFill"] = {
                "timeInForce": "GTC",
                "price": str(round(take_profit, 5))
            }
            
        # Execute the order
        url = f"{config.oanda_api_url}/v3/accounts/{config.oanda_account}/orders"
        headers = {
            "Authorization": f"Bearer {config.oanda_api_key}",
            "Content-Type": "application/json"
        }
        
        session = await get_session()
        async with session.post(url, json=order_data, headers=headers) as response:
            result = await response.json()
            
            if response.status == 201:
                logger.info(f"Order executed successfully: {result}")
                return True, result
            else:
                logger.error(f"Order execution failed: {result}")
                return False, result
                
    except Exception as e:
        logger.error(f"Error executing trade: {str(e)}")
        return False, {"error": str(e)}

@handle_async_errors
async def execute_trade_close(alert: AlertData) -> Tuple[bool, Dict[str, Any]]:
    """Close a trade completely"""
    try:
        logger.info(f"Closing position for {alert.symbol}")
        
        # Prepare close data
        close_data = {
            "longUnits": "ALL",
            "shortUnits": "ALL"
        }
        
        # Execute the close
        url = f"{config.oanda_api_url}/v3/accounts/{config.oanda_account}/positions/{standardize_symbol(alert.symbol)}/close"
        headers = {
            "Authorization": f"Bearer {config.oanda_api_key}",
            "Content-Type": "application/json"
        }
        
        session = await get_session()
        async with session.put(url, json=close_data, headers=headers) as response:
            result = await response.json()
            
            if response.status in [200, 201]:
                logger.info(f"Position closed successfully: {result}")
                return True, result
            else:
                logger.error(f"Position close failed: {result}")
                return False, result
                
    except Exception as e:
        logger.error(f"Error closing trade: {str(e)}")
        return False, {"error": str(e)}

@handle_async_errors
async def execute_trade_partial_close(alert: AlertData, percentage: float) -> Tuple[bool, Dict[str, Any]]:
    """Close a portion of a trade"""
    try:
        logger.info(f"Partially closing position for {alert.symbol}: {percentage}%")
        
        # First, get the position to determine current units
        url = f"{config.oanda_api_url}/v3/accounts/{config.oanda_account}/positions/{standardize_symbol(alert.symbol)}"
        headers = {
            "Authorization": f"Bearer {config.oanda_api_key}",
            "Content-Type": "application/json"
        }
        
        session = await get_session()
        async with session.get(url, headers=headers) as response:
            position_data = await response.json()
            
            if response.status != 200:
                logger.error(f"Error fetching position details: {position_data}")
                return False, {"error": "Failed to get position details"}
                
            # Extract units
            long_units = float(position_data.get('position', {}).get('long', {}).get('units', 0))
            short_units = float(position_data.get('position', {}).get('short', {}).get('units', 0))
            
            # Calculate units to close
            if long_units > 0:
                units_to_close = int(long_units * percentage / 100)
                close_data = {
                    "longUnits": str(units_to_close)
                }
            elif short_units < 0:
                units_to_close = int(abs(short_units) * percentage / 100)
                close_data = {
                    "shortUnits": str(units_to_close)
                }
            else:
                logger.error(f"No position found for {alert.symbol}")
                return False, {"error": "No position found"}
                
        # Execute the partial close
        close_url = f"{config.oanda_api_url}/v3/accounts/{config.oanda_account}/positions/{standardize_symbol(alert.symbol)}/close"
        
        async with session.put(close_url, json=close_data, headers=headers) as response:
            result = await response.json()
            
            if response.status in [200, 201]:
                logger.info(f"Position partially closed successfully: {result}")
                return True, result
            else:
                logger.error(f"Position partial close failed: {result}")
                return False, result
                
    except Exception as e:
        logger.error(f"Error in partial position close: {str(e)}")
        return False, {"error": str(e)}

async def is_market_open(symbol: str) -> bool:
    """Check if market is open for the symbol"""
    # Basic check for forex
    if '_' in symbol or symbol.endswith('JPY'):
        # Forex is open 24/5
        now = datetime.now(timezone('Asia/Bangkok'))
        day_of_week = now.weekday()
        
        # Monday to Friday (0-4)
        if day_of_week < 5:
            return True
            
        # Weekend
        return False
    
    # For other instruments, would need more complex logic
    # For now, assuming forex-like hours
    return True

async def is_tradeable(symbol: str) -> bool:
    """Check if instrument is tradeable"""
    # For a more complex implementation, would check with broker API
    # For now, using a simplified approach
    
    # Convert symbol format if needed
    instrument = symbol.replace('_', '_')


    
    # List of untradeable instruments
    untradeable = [
        # Add any known untradeable symbols
    ]
    
    return instrument not in untradeable

# Utility to standardize symbol format
def standardize_symbol(symbol: str) -> str:
    """Standardize symbol format"""
    # Handle different formats (USD/JPY, USD_JPY, USDJPY)
    if '/' in symbol:
        parts = symbol.split('/')
        return f"{parts[0]}_{parts[1]}"
    elif '_' not in symbol and len(symbol) == 6:
        # USDJPY -> USD_JPY
        return f"{symbol[:3]}_{symbol[3:]}"
    
    return symbol

# Main function to run the application
async def main():
    """Main entry point for the application"""
    # Configure logging
    config_logging()
    
    # Print startup banner
    print("""
    ╔════════════════════════════════════════╗
    ║                                        ║
    ║     Bot-i-Dharma Trading System        ║
    ║     Version 1.0.0                      ║
    ║                                        ║
    ╚════════════════════════════════════════╝
    """)
    
    logger.info("Starting Bot-i-Dharma Trading System")
    
    # Initialize database
    await init_db()
    
    # Start position manager
    await position_manager.start()
    
    # Start web server using uvicorn
    config = uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=8000,
        log_level="info"
    )
    server = uvicorn.Server(config)
    
    try:
        await server.serve()
    except KeyboardInterrupt:
        logger.info("Shutting down server...")
    except Exception as e:
        logger.error(f"Server error: {str(e)}")
    finally:
        # Cleanup
        await position_manager.stop()
        await close_db_connection()
        await close_all_sessions()
        
        logger.info("Trading system shutdown complete")

if __name__ == "__main__":
    # Run main function
    asyncio.run(main()) 
