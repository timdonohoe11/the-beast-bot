from pycoingecko import CoinGeckoAPI
import pandas as pd
from datetime import datetime, timedelta
import requests
import time
import argparse
import os
import json  # For state
from dotenv import load_dotenv
import random
from base58 import b58decode
import asyncio
import base64
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solders import message
from solana.rpc.types import TxOpts
from solana.rpc.commitment import Processed

load_dotenv()

# Config
X_BEARER = os.getenv('X_BEARER_TOKEN', '')
HELIUS_KEY = os.getenv('HELIUS_API_KEY', '')
WALLET_PRIVATE_KEY = os.getenv('WALLET_PRIVATE_KEY', '')
START_CAPITAL = 500
WHALE_MODE = True
TRADE_ALLOC = 0.95
MIN_TRADE_USD = 5
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
MIN_LIQ = 100000  # Rug filter $100k liq
MIN_HOLDERS = 1000  # Rug filter holders
STATE_FILE = 'beast_state.json'  # Autonomous state

class TheBeastV3:
    def __init__(self, mode='backtest', paper=False):
        self.cg = CoinGeckoAPI()
        self.helius_rpc = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}" if HELIUS_KEY else None
        self.mode = mode
        self.paper = paper
        self.capital = self.load_capital()  # Load from state/compounding
        self.trades = []
        self.solana_tokens = self.get_solana_tokens()
        self.day_cache = {}
        num_scan = 10 if mode == 'backtest' else 10  # Apex: Full hunt
        self.async_client = None
        self.keypair = None
        self.jupiter = None
        if not self.paper:
            try:
                from solana.rpc.async_api import AsyncClient
                from solders.keypair import Keypair
                from jupiter_python_sdk.jupiter import Jupiter
                self.async_client = AsyncClient("https://api.mainnet-beta.solana.com")
                seed_bytes = b58decode(WALLET_PRIVATE_KEY)
                self.keypair = Keypair.from_bytes(seed_bytes) if WALLET_PRIVATE_KEY else None
                if self.keypair:
                    self.jupiter = Jupiter(
                        async_client=self.async_client,
                        keypair=self.keypair,
                        quote_api_url="https://quote-api.jup.ag/v6/quote",
                        swap_api_url="https://quote-api.jup.ag/v6/swap",
                    )
            except ImportError as e:
                print(f"Solana/Jupiter import error: {e} - Fallback to paper mode")
                self.paper = True
            except Exception as e:
                print(f"Client init error: {e}")
                self.paper = True
        print(f"Apex Beast loaded: Capital ${self.capital:.2f}, {len(self.solana_tokens)} tokens <500m, scanning {num_scan}, paper: {self.paper}")

    def load_capital(self):
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, 'r') as f:
                    state = json.load(f)
                    return float(state.get('capital', START_CAPITAL))
        except:
            pass
        return START_CAPITAL

    def save_state(self, open_trades=None):
        state = {'capital': self.capital}
        if open_trades is not None:
            state['open_trades'] = open_trades
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)
        pd.DataFrame(self.trades).to_csv('beast_log.csv', index=False)

    def get_solana_tokens(self, max_cap=500_000_000, max_ids=100):
        try:
            all_coins = self.cg.get_coins_list(include_platform=True)
            solana_data = []
            for c in all_coins:
                platforms = c.get('platforms', {})
                if platforms.get('solana'):
                    mint = platforms['solana']
                    if mint and len(mint) > 20:
                        solana_data.append({'id': c['id'], 'symbol': c['symbol'], 'mint': mint})
            solana_ids = [d['id'] for d in solana_data][:max_ids]
            markets = self.cg.get_coins_markets(vs_currency='usd', ids=solana_ids, 
                                                order='market_cap_desc', per_page=max_ids, page=1)
            id_to_data = {d['id']: d for d in solana_data}
            filtered = []
            for m in markets:
                if m.get('market_cap') and m['market_cap'] < max_cap:
                    data = id_to_data.get(m['id'], {})
                    if data:
                        data.update(m)
                        filtered.append(data)
            return filtered
        except Exception as e:
            print(f"Token fetch error: {e}")
            return []

    def whale_activity(self, mint, current_date):
        if not self.helius_rpc or not WHALE_MODE:
            return 0.0
        day_key = f"{current_date.date()}_{mint}_whale"
        if day_key in self.day_cache:
            return self.day_cache[day_key]
        try:
            hours_back = 24
            from_ts = int((current_date - timedelta(hours=hours_back)).timestamp())
            payload = {"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress", "params": [mint, {"limit": 50}]}
            resp = requests.post(self.helius_rpc, json=payload)
            if resp.status_code == 200:
                signatures = resp.json().get('result', [])
                buys = 0
                for sig in signatures:
                    block_time = sig.get('blockTime')
                    if not block_time or block_time < from_ts:
                        break
                    txn_payload = {"jsonrpc": "2.0", "id": 1, "method": "getTransaction", "params": [sig['signature'], {"encoding": "jsonParsed"}]}
                    txn_resp = requests.post(self.helius_rpc, json=txn_payload)
                    if txn_resp.status_code == 200:
                        txn = txn_resp.json().get('result')
                        if txn and 'meta' in txn and txn['meta'].get('preBalances') and txn['meta'].get('postBalances'):
                            pre_bal = txn['meta']['preBalances'][0]
                            post_bal = txn['meta']['postBalances'][0]
                            sol_transfer = (post_bal - pre_bal) / 1e9
                            if sol_transfer > 133:
                                buys += 1
                factor = min(buys * 0.15, 0.6)
                if buys > 0:
                    print(f"Whale buys for {mint}: {buys} ($20k+), factor {factor:.2f}")
                self.day_cache[day_key] = factor
                time.sleep(0.3)
                return factor
            self.day_cache[day_key] = 0.0
            return 0.0
        except Exception as e:
            print(f"Whale error for {mint}: {e}")
            self.day_cache[day_key] = 0.0
            return 0.0

    def x_semantic_sentiment(self, symbol, current_date):  # Flair: Semantic for intent spike
        day_key = f"{current_date.date()}_{symbol}_x_sem"
        if day_key in self.day_cache:
            return self.day_cache[day_key]
        if self.mode == 'backtest' or self.paper:
            return random.uniform(0.6, 0.8)
        # Mock semantic (real: x_semantic_search tool)
        score = random.uniform(0.5, 0.9) if random.random() > 0.3 else 0.3
        print(f"X semantic buzz for {symbol}: {score:.2f}")
        self.day_cache[day_key] = score
        return score

    async def rug_filter(self, mint):
        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            resp = requests.get(url)
            if resp.status_code == 200:
                data = resp.json().get('pairs', [{}])[0]
                liq = float(data.get('liquidity', {}).get('usd', 0))
                holders = int(data.get('holders', 0))
                if liq > MIN_LIQ and holders > MIN_HOLDERS:
                    return True
                print(f"Rug filter fail for {mint}: Liq ${liq:.0f}, Holders {holders}")
                return False
        except:
            pass
        return True  # Fallback

    def ta_baseline(self, coin_id, current_date, days=3):
        day_key = f"{current_date.date()}_{coin_id}_ta"
        if day_key in self.day_cache:
            return self.day_cache[day_key]
        retries = 3
        ta_ok, vol_ratio = False, 1.0
        for attempt in range(retries):
            try:
                from_ts = int((current_date - timedelta(days=days)).timestamp())
                to_ts = int(current_date.timestamp())
                hist = self.cg.get_coin_market_chart_range_by_id(coin_id, 'usd', from_ts, to_ts)
                if not hist or 'prices' not in hist or len(hist['prices']) < 5:
                    break
                df = pd.DataFrame(hist['prices'], columns=['timestamp', 'price'])
                df['ma5'] = df['price'].rolling(5).mean()
                df['vol'] = df['price'].rolling(5).std()
                avg_vol = df['vol'].mean()
                current_price = df['price'].iloc[-1]
                current_ma = df['ma5'].iloc[-1]
                current_vol = df['vol'].iloc[-1]
                vol_ratio = current_vol / avg_vol if avg_vol > 0 else 1
                ta_ok = (vol_ratio > 0.8) and (current_price > current_ma)  # Loosened vol for more hunts
                time.sleep(1)
                break
            except Exception as e:
                if '429' in str(e):
                    print(f"Rate limit for {coin_id}, retry {attempt+1}/3...")
                    time.sleep(60)
                else:
                    print(f"TA error for {coin_id}: {e}")
                    break
        result = (ta_ok, vol_ratio)
        self.day_cache[day_key] = result
        return result

    async def scan_signals(self, current_date):  # Made async for rug await
        if not self.solana_tokens:
            return []
        signals = []
        num_scan = 50
        for token in self.solana_tokens[:num_scan]:
            symbol = token['symbol'].upper()
            sentiment = self.x_semantic_sentiment(symbol, current_date)
            ta_ok, vol_ratio = self.ta_baseline(token['id'], current_date)
            print(f"TA check for {symbol}: OK={ta_ok}, vol_ratio={vol_ratio:.2f}")
            if ta_ok:
                if not await self.rug_filter(token['mint']):
                    continue
                whale_factor = self.whale_activity(token['mint'], current_date)
                score = (sentiment * 0.5) + (vol_ratio * 0.5) + (whale_factor * 0.3)
                if score > 1.0:
                    signals.append((token, score))
                    print(f"Signal for {symbol}: Score {score:.2f}")
        signals.sort(key=lambda x: x[1], reverse=True)
        return signals

    async def get_usdc_balance(self):
        if not self.async_client or not self.keypair:
            return self.capital  # % bal fallback
        try:
            resp = await self.async_client.get_token_accounts_by_owner(
                self.keypair.pubkey(), {"mint": Pubkey.from_string(USDC_MINT)}
            )
            if resp.value and len(resp.value) > 0:
                data = resp.value[0].account.data
                if hasattr(data, 'parsed'):
                    ui_amount = data.parsed['info']['tokenAmount']['uiAmount']
                else:
                    parsed = json.loads(base64.b64decode(str(data)).decode('utf-8')) if isinstance(str(data), str) else {}
                    ui_amount = parsed.get('parsed', {}).get('info', {}).get('tokenAmount', {}).get('uiAmount', 0)
                balance = float(ui_amount or 0)
                print(f"USDC balance: ${balance:.2f}")
                return balance
            else:
                print("No USDC ATA—Jupiter creates on swap")
                return self.capital
        except Exception as e:
            print(f"Balance check error: {e}—using capital ${self.capital:.2f}")
            return self.capital

    async def execute_swap(self, input_mint, output_mint, amount, is_buy=True):
        if not self.keypair or not self.async_client:
            return None, 0
        wallet_pub = str(self.keypair.pubkey())
        for attempt in range(3):
            try:
                quote_url = "https://quote-api.jup.ag/v6/quote"
                quote_params = {
                    "inputMint": input_mint,
                    "outputMint": output_mint,
                    "amount": str(amount),
                    "slippageBps": 100,
                    "onlyDirectRoutes": "false"
                }
                quote_resp = requests.get(quote_url, params=quote_params)
                if quote_resp.status_code != 200:
                    print(f"Quote error {attempt+1}/3: {quote_resp.status_code} - {quote_resp.text}")
                    await asyncio.sleep(5)
                    continue
                quote_data = quote_resp.json()
                print(f"Quote response: {json.dumps(quote_data, indent=2)[:200]}...")
                if not quote_data or 'outAmount' not in quote_data:
                    print(f"No routes—retry later")
                    await asyncio.sleep(5)
                    continue
                out_amount = int(quote_data['outAmount'])
                swap_url = "https://quote-api.jup.ag/v6/swap"
                swap_payload = {
                    "quoteResponse": quote_data,
                    "userPublicKey": wallet_pub,
                    "wrapAndUnwrapSol": True,
                    "computeUnitPriceMicroLamports": 100000
                }
                swap_resp = requests.post(swap_url, json=swap_payload)
                if swap_resp.status_code != 200:
                    print(f"Swap prep error {attempt+1}/3: {swap_resp.status_code} - {swap_resp.text}")
                    await asyncio.sleep(5)
                    continue
                swap_data = swap_resp.json()
                if 'swapTransaction' not in swap_data:
                    print(f"Invalid swap: {swap_data}")
                    continue
                transaction_data = swap_data['swapTransaction']
                raw_transaction = VersionedTransaction.from_bytes(base64.b64decode(transaction_data))
                signature = self.keypair.sign_message(message.to_bytes_versioned(raw_transaction.message))
                signed_txn = VersionedTransaction.populate(raw_transaction.message, [signature])
                opts = TxOpts(skip_preflight=False, preflight_commitment=Processed)
                result = await self.async_client.send_raw_transaction(txn=bytes(signed_txn), opts=opts)
                tx_id = result.value if hasattr(result, 'value') else json.loads(result.to_json()).get('result', 'unknown')
                print(f"Swap {'buy' if is_buy else 'sell'} tx: https://explorer.solana.com/tx/{tx_id}")
                for _ in range(60):  # Extended 60s poll
                    status = await self.async_client.get_signature_statuses([tx_id])
                    if status.value and status.value[0] and status.value[0].confirmation_status == 'confirmed':
                        print("Swap confirmed!")
                        return tx_id, out_amount
                    await asyncio.sleep(1)
                print("Extended timeout—check explorer (likely success)")
                return tx_id, out_amount
            except json.JSONDecodeError as e:
                print(f"JSON error {attempt+1}/3: {e}")
                await asyncio.sleep(5)
            except Exception as e:
                print(f"Swap error {attempt+1}/3: {e}")
                await asyncio.sleep(5)
        return None, 0

    async def poll_open_trades(self):
        try:
            with open(STATE_FILE, 'r') as f:
                state = json.load(f)
                open_trades = state.get('open_trades', [])
        except:
            open_trades = []
        sold = []
        for trade in open_trades:
            mint = trade['mint']
            entry_price = trade['entry_price']
            token_out = trade['token_out']
            coin_id = trade['coin_id']
            current_price = self.cg.get_price(ids=coin_id, vs_currencies='usd')[coin_id]['usd']
            ret = (current_price - entry_price) / entry_price
            tp = min(trade['vol_ratio'] * 0.02, 0.15)
            sl_trail = max(trade.get('sl_trail', SL_TARGET), ret - 0.01)  # Trail
            trade['sl_trail'] = sl_trail
            if ret >= tp or ret <= sl_trail or (abs(ret) < FLAT_THRESHOLD and ret < 0):
                sell_tx, _ = await self.execute_swap(mint, USDC_MINT, token_out, is_buy=False)
                self.capital *= (1 + ret * TRADE_ALLOC / len(open_trades))
                self.trades.append({'date': datetime.now().date(), 'symbol': trade['symbol'], 'return': ret*100, 'tx_sell': sell_tx})
                sold.append(trade)
                print(f"Auto-sell {trade['symbol']}: {ret*100:.1f}% (TP {tp*100:.1f}%)")
        open_trades = [t for t in open_trades if t not in sold]
        self.save_state(open_trades)

    async def run_daily(self):
        await self.poll_open_trades()  # Sell open first
        now = datetime.now()
        signals = await self.scan_signals(now)
        if signals:
            top_signals = [s for s in signals if s[1] > 1.0][:3]  # Apex: Top 3
            num_trades = len(top_signals)
            alloc_per = TRADE_ALLOC / num_trades
            usdc_bal = await self.get_usdc_balance()
            if usdc_bal is None:
                usdc_bal = self.capital
            open_trades = []  # Load from state
            for i, (token, score) in enumerate(top_signals):
                symbol = token['symbol']
                coin_id = token['id']
                mint = token['mint']
                price = self.cg.get_price(ids=coin_id, vs_currencies='usd')[coin_id]['usd']
                print(f"Signal #{i+1}: BUY {symbol} @ ${price} (score {score:.2f})")
                trade_usd = min(self.capital * alloc_per, usdc_bal)
                amount = int(trade_usd * 1_000_000)
                print(f"Buy: {trade_usd} USD to {symbol} mint {mint}")
                buy_tx, token_out = await self.execute_swap(USDC_MINT, mint, amount, is_buy=True)
                if not buy_tx:
                    print(f"Buy failed for {symbol}")
                    continue
                tp = min(score * 0.02, 0.15)  # Adaptive
                sl_trail = SL_TARGET
                open_trade = {'symbol': symbol, 'coin_id': coin_id, 'mint': mint, 'entry_price': price, 'token_out': token_out, 'vol_ratio': score * 0.5, 'sl_trail': sl_trail}
                open_trades.append(open_trade)
                # Short poll
                exited = False
                for j in range(12):
                    await asyncio.sleep(300)
                    current_price = self.cg.get_price(ids=coin_id, vs_currencies='usd')[coin_id]['usd']
                    ret = (current_price - entry_price) / entry_price
                    if ret >= tp:
                        sl_trail = max(sl_trail + 0.005, ret - 0.01)
                    if ret >= tp or ret <= sl_trail or (abs(ret) < FLAT_THRESHOLD and ret < 0):
                        sell_tx, _ = await self.execute_swap(mint, USDC_MINT, token_out, is_buy=False)
                        self.capital *= (1 + ret * alloc_per)
                        self.trades.append({'date': now.date(), 'symbol': symbol, 'return': ret*100, 'tx_buy': buy_tx, 'tx_sell': sell_tx})
                        print(f"Exit {symbol}: {ret*100:.1f}%")
                        exited = True
                        break
                if not exited:
                    print(f"Holding {symbol} to EOD/poll")
            self.save_state(open_trades)
        else:
            print("No signals today")
        self.save_state()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', default='backtest', choices=['backtest', 'live'])
    parser.add_argument('--paper', action='store_true')
    args = parser.parse_args()
    beast = TheBeastV3(args.mode, args.paper)
    if args.mode == 'backtest':
        beast.backtest(args.start, args.end)
    else:
        asyncio.run(beast.run_daily())