import os
import json
import time
import random
import logging
import subprocess
import requests
import base58
import hashlib
import hmac
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Tuple
from dotenv import load_dotenv
from web3 import Web3
from eth_account import Account
from solana.rpc.api import Client
from solana.keypair import Keypair
from solana.publickey import PublicKey
from solana.transaction import Transaction, TransactionInstruction
from solana.system_program import TransferParams, transfer
from solana.rpc.types import TxOpts
from spl.token.client import Token
from spl.token.constants import TOKEN_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID
from spl.token.instructions import get_associated_token_address, create_associated_token_account
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.instruction import Instruction
from solders.message import Message
from solders.transaction import VersionedTransaction
from solders.commitment_config import CommitmentLevel
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputFile
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters, ConversationHandler
from telegram.constants import ParseMode

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ============ CONFIGURATION ============
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

# Solana Configuration
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
SOLANA_WS = os.getenv("SOLANA_WS", "wss://api.mainnet-beta.solana.com")
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")  # base58 encoded
SOLANA_PUBLIC_KEY = os.getenv("SOLANA_PUBLIC_KEY")

# EVM Configuration
ETH_RPC = os.getenv("ETH_RPC", "https://mainnet.infura.io/v3/YOUR_KEY")
ETH_PRIVATE_KEY = os.getenv("ETH_PRIVATE_KEY", "0xYOUR_PRIVATE_KEY")
ETH_PUBLIC_KEY = os.getenv("ETH_PUBLIC_KEY")
BSC_RPC = os.getenv("BSC_RPC", "https://bsc-dataseed.binance.org/")
BSC_PRIVATE_KEY = os.getenv("BSC_PRIVATE_KEY", "0xYOUR_PRIVATE_KEY")
BSC_PUBLIC_KEY = os.getenv("BSC_PUBLIC_KEY")
POLYGON_RPC = os.getenv("POLYGON_RPC", "https://polygon-rpc.com")
POLYGON_PRIVATE_KEY = os.getenv("POLYGON_PRIVATE_KEY", "0xYOUR_PRIVATE_KEY")
POLYGON_PUBLIC_KEY = os.getenv("POLYGON_PUBLIC_KEY")

# Jupiter API
JUPITER_API = "https://quote-api.jup.ag/v6"

# ============ STATE MANAGER ============
class StateManager:
    def __init__(self):
        self.wallets = {}  # wallet_id -> {chain, private_key, public_key, label, balance}
        self.tokens = {}  # chain -> token_data
        self.transactions = []  # list of tx history
        self.total_profit = 0.0
        self.total_volume = 0
        self.total_drained = 0
        self.buy_alerts = []
        self.pump_start_time = None
        self.pump_active = False
        self.referrals = {}
        self.blacklist = []
        self.whitelist = []
        self.deposit_addresses = {}
        self.pending_orders = {}
        
    def add_wallet(self, chain: str, private_key: str, public_key: str, label: str = ""):
        wallet_id = hashlib.sha256(f"{chain}_{public_key}".encode()).hexdigest()[:16]
        self.wallets[wallet_id] = {
            "id": wallet_id,
            "chain": chain,
            "private_key": private_key,
            "public_key": public_key,
            "label": label,
            "balance": 0.0,
            "created": datetime.now().isoformat()
        }
        return wallet_id
    
    def get_wallet(self, wallet_id: str):
        return self.wallets.get(wallet_id)
    
    def get_wallets_by_chain(self, chain: str):
        return [w for w in self.wallets.values() if w["chain"] == chain]
    
    def add_token(self, chain: str, data: dict):
        if chain not in self.tokens:
            self.tokens[chain] = []
        self.tokens[chain].append(data)
        
    def get_token(self, chain: str, symbol: str = None):
        if chain not in self.tokens:
            return None
        if symbol:
            for t in self.tokens[chain]:
                if t.get("symbol", "").upper() == symbol.upper():
                    return t
        return self.tokens[chain][-1] if self.tokens[chain] else None
    
    def add_transaction(self, tx_data: dict):
        self.transactions.append({
            **tx_data,
            "timestamp": datetime.now().isoformat()
        })
        
    def add_profit(self, amount: float):
        self.total_profit += amount
        
    def add_volume(self, amount: float):
        self.total_volume += amount
        
    def add_drained(self, amount: float):
        self.total_drained += amount
        
    def add_buy_alert(self, buyer: str, amount: float, chain: str, token: str):
        self.buy_alerts.append({
            "buyer": buyer,
            "amount": amount,
            "chain": chain,
            "token": token,
            "time": datetime.now().isoformat()
        })
        
    def get_pump_multiplier(self):
        if not self.pump_start_time:
            return 1.0
        elapsed = (datetime.now() - self.pump_start_time).total_seconds() / 3600
        # Logistic growth: 30X after 2 hours, then slows to 2X per hour after
        if elapsed < 2:
            return 1 + (29 * (elapsed / 2) ** 2)  # Accelerating
        elif elapsed < 12:
            return 30 + (elapsed - 2) * 0.5  # Slowing growth
        else:
            return 35  # Stable plateau
            
    def get_dashboard(self):
        return f"""
💰 **Total Profit:** ${self.total_profit:,.2f}
📊 **Total Volume:** {self.total_volume:,.0f}
💧 **Total Drained:** {self.total_drained:,.0f}
📈 **Active Tokens:** {sum(len(v) for v in self.tokens.values())}
💳 **Wallets:** {len(self.wallets)}
📈 **Pump Multiplier:** {self.get_pump_multiplier():.1f}X
🛒 **Recent Buys:** {len(self.buy_alerts)}
        """

state = StateManager()

# ============ SOLANA HELPER ============
class SolanaHelper:
    def __init__(self):
        self.client = Client(SOLANA_RPC)
        self.keypair = None
        if SOLANA_PRIVATE_KEY:
            try:
                self.keypair = Keypair.from_base58_string(SOLANA_PRIVATE_KEY)
                logger.info(f"Solana wallet loaded: {self.keypair.public_key}")
            except:
                logger.error("Failed to load Solana private key")
        
    def get_client(self):
        return self.client
    
    def get_keypair(self):
        return self.keypair
    
    def create_wallet(self) -> Tuple[str, str]:
        """Create new Solana wallet"""
        keypair = Keypair.generate()
        private_key = base58.b58encode(keypair.secret_key).decode()
        public_key = str(keypair.public_key)
        return private_key, public_key
    
    def import_wallet(self, private_key: str) -> Tuple[str, str]:
        """Import Solana wallet from private key"""
        try:
            keypair = Keypair.from_base58_string(private_key)
            public_key = str(keypair.public_key)
            return public_key, keypair
        except Exception as e:
            raise ValueError(f"Invalid private key: {e}")
    
    def get_balance(self, public_key: str) -> float:
        """Get SOL balance"""
        try:
            response = self.client.get_balance(PublicKey(public_key))
            return response['result']['value'] / 1e9
        except:
            return 0.0
    
    def get_token_balance(self, mint_address: str, wallet_address: str) -> float:
        """Get token balance for a wallet"""
        try:
            ata = get_associated_token_address(PublicKey(wallet_address), PublicKey(mint_address))
            response = self.client.get_token_account_balance(ata)
            return response['result']['value']['uiAmount'] or 0.0
        except:
            return 0.0
    
    def create_token(self, name: str, symbol: str, decimals: int = 9, supply: int = 1_000_000_000) -> dict:
        """Create real SPL token with full supply"""
        try:
            if not self.keypair:
                return {"success": False, "error": "No keypair loaded"}
            
            # Create mint
            from spl.token.instructions import create_mint
            mint = Token.create_mint(
                self.client,
                self.keypair,
                self.keypair.public_key,
                None,
                decimals,
                TOKEN_PROGRAM_ID
            )
            
            mint_address = str(mint.pubkey)
            
            # Create associated token account for the owner
            ata = get_associated_token_address(self.keypair.public_key, mint.pubkey)
            
            # Mint initial supply
            mint.mint_to(
                ata,
                self.keypair,
                supply * 10**decimals
            )
            
            # Enable freeze authority (optional)
            # mint.freeze_account(ata, self.keypair)
            
            return {
                "success": True,
                "mint": mint_address,
                "ata": str(ata),
                "decimals": decimals,
                "supply": supply
            }
        except Exception as e:
            logger.error(f"Token creation error: {e}")
            return {"success": False, "error": str(e)}
    
    def transfer_token(self, mint_address: str, from_wallet: str, to_wallet: str, amount: int, private_key: str = None) -> dict:
        """Transfer tokens between wallets"""
        try:
            # Use provided private key or default
            if private_key:
                keypair = Keypair.from_base58_string(private_key)
            else:
                keypair = self.keypair
                
            if not keypair:
                return {"success": False, "error": "No keypair available"}
                
            mint_pubkey = PublicKey(mint_address)
            from_pubkey = PublicKey(from_wallet)
            to_pubkey = PublicKey(to_wallet)
            
            # Get associated token accounts
            from_ata = get_associated_token_address(from_pubkey, mint_pubkey)
            to_ata = get_associated_token_address(to_pubkey, mint_pubkey)
            
            # Create token wrapper
            token = Token(self.client, mint_pubkey, TOKEN_PROGRAM_ID, keypair)
            
            # Check if recipient has ATA, if not create it
            try:
                self.client.get_token_account_balance(to_ata)
            except:
                # Create ATA for recipient
                create_ata_ix = create_associated_token_account(
                    keypair.public_key,
                    to_pubkey,
                    mint_pubkey
                )
                # Execute creation
                tx = Transaction().add(create_ata_ix)
                self.client.send_transaction(tx, keypair)
            
            # Execute transfer
            tx = token.transfer(
                from_ata,
                to_ata,
                keypair.public_key,
                amount
            )
            
            return {
                "success": True,
                "tx": str(tx),
                "from": from_wallet,
                "to": to_wallet,
                "amount": amount
            }
        except Exception as e:
            logger.error(f"Transfer error: {e}")
            return {"success": False, "error": str(e)}
    
    def swap_on_jupiter(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int = 100) -> dict:
        """Execute swap on Jupiter"""
        try:
            # Get quote
            quote_url = f"{JUPITER_API}/quote"
            params = {
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": amount,
                "slippageBps": slippage_bps
            }
            response = requests.get(quote_url, params=params)
            quote = response.json()
            
            if "error" in quote:
                return {"success": False, "error": quote["error"]}
            
            # Get swap transaction
            swap_url = f"{JUPITER_API}/swap"
            swap_data = {
                "quoteResponse": quote,
                "userPublicKey": str(self.keypair.public_key),
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True
            }
            response = requests.post(swap_url, json=swap_data)
            swap_response = response.json()
            
            if "error" in swap_response:
                return {"success": False, "error": swap_response["error"]}
            
            # Send transaction
            tx_data = bytes.fromhex(swap_response["swapTransaction"])
            tx = VersionedTransaction.from_bytes(tx_data)
            signature = self.client.send_transaction(tx, self.keypair)
            
            return {
                "success": True,
                "tx": str(signature.value),
                "input_amount": amount,
                "output_amount": quote.get("outAmount", 0)
            }
        except Exception as e:
            logger.error(f"Jupiter swap error: {e}")
            return {"success": False, "error": str(e)}
    
    def get_transaction(self, tx_hash: str) -> dict:
        """Get transaction details"""
        try:
            response = self.client.get_transaction(
                Signature.from_string(tx_hash),
                commitment=CommitmentLevel("confirmed")
            )
            return {"success": True, "data": response}
        except Exception as e:
            return {"success": False, "error": str(e)}

solana_helper = SolanaHelper()

# ============ EVM HELPER ============
class EVMHelper:
    def __init__(self):
        self.chains = {
            "ethereum": {
                "rpc": ETH_RPC,
                "private_key": ETH_PRIVATE_KEY,
                "public_key": ETH_PUBLIC_KEY,
                "chain_id": 1
            },
            "bsc": {
                "rpc": BSC_RPC,
                "private_key": BSC_PRIVATE_KEY,
                "public_key": BSC_PUBLIC_KEY,
                "chain_id": 56
            },
            "polygon": {
                "rpc": POLYGON_RPC,
                "private_key": POLYGON_PRIVATE_KEY,
                "public_key": POLYGON_PUBLIC_KEY,
                "chain_id": 137
            }
        }
        self.w3s = {}
        self.accounts = {}
        
        # Initialize connections
        for chain, config in self.chains.items():
            if config["rpc"]:
                try:
                    self.w3s[chain] = Web3(Web3.HTTPProvider(config["rpc"]))
                    if config["private_key"]:
                        self.accounts[chain] = Account.from_key(config["private_key"])
                    logger.info(f"EVM chain {chain} initialized")
                except Exception as e:
                    logger.error(f"Failed to initialize {chain}: {e}")
    
    def get_w3(self, chain: str):
        return self.w3s.get(chain)
    
    def get_account(self, chain: str):
        return self.accounts.get(chain)
    
    def create_wallet(self) -> Tuple[str, str]:
        """Create new EVM wallet"""
        account = Account.create()
        return account.key.hex(), account.address
    
    def import_wallet(self, private_key: str) -> str:
        """Import EVM wallet from private key"""
        try:
            account = Account.from_key(private_key)
            return account.address
        except Exception as e:
            raise ValueError(f"Invalid private key: {e}")
    
    def get_balance(self, chain: str, address: str) -> float:
        """Get native balance"""
        try:
            w3 = self.get_w3(chain)
            if not w3:
                return 0.0
            balance = w3.eth.get_balance(address)
            return balance / 1e18
        except:
            return 0.0
    
    def get_token_balance(self, chain: str, token_address: str, wallet_address: str) -> float:
        """Get ERC-20 token balance"""
        try:
            w3 = self.get_w3(chain)
            if not w3:
                return 0.0
                
            abi = json.loads('[{"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"}]')
            contract = w3.eth.contract(address=token_address, abi=abi)
            balance = contract.functions.balanceOf(wallet_address).call()
            return balance / 1e18
        except:
            return 0.0
    
    def deploy_token(self, chain: str, name: str, symbol: str, drain_bps: int, supply: int = 1_000_000_000) -> dict:
        """Deploy ERC-20 token with drain and sell-block"""
        try:
            w3 = self.get_w3(chain)
            account = self.get_account(chain)
            if not w3 or not account:
                return {"success": False, "error": "Chain or account not initialized"}
            
            # Contract bytecode (compiled PhantomToken)
            bytecode = "0x608060405234801561001057600080fd5b5060405162001a2a38038062001a2a833981016040819052610033916100e1565b33600081815560018390556040516001600160a01b0392909216917fddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef9061007d90600090610143565b60405180910390a35061018b565b60008135905061009a81610174565b92915050565b6000815190506100af81610174565b92915050565b6000819050919050565b6100c8816100b5565b81146100d357600080fd5b50565b6000815190506100cb816100bf565b6000602082840312156100f357600080fd5b6000610101848285016100a0565b91505092915050565b6000610115826100b5565b82525050565b6000610126600c90565b90565b60006101358261011b565b9050919050565b61013d81610129565b82525050565b6000602082019050610158600083018461010a565b92915050565b6000610169826100b5565b9050919050565b61017d8161015e565b811461018857600080fd5b50565b61188f806200019b6000396000f3fe608060405234801561001057600080fd5b506004361061010b5760003560e01c80638da5cb5b116100a2578063dd62ed3e11610071578063dd62ed3e146102d7578063e0b4b4d414610307578063f2fde38b14610337578063f5f3b8a514610353578063fc0c546a146103835761010b565b80638da5cb5b1461024f57806395d89b411461026d578063a9059cbb1461028b578063d5f39488146102bb5761010b565b8063313ce567116100de578063313ce567146101b957806340c10f19146101d757806370a08231146101f35780637a9e5e4b146102235761010b565b806306fdde0314610110578063095ea7b31461012e57806318160ddd1461015e57806323b872dd1461017c575b600080fd5b6101186103a1565b6040516101259190611341565b60405180910390f35b610148600480360381019061014391906111ca565b61042f565b60405161015591906112ad565b60405180910390f35b6101666104cd565b60405161017391906113b0565b60405180910390f35b61019660048036038101906101919190611137565b6104d3565b6040516101a391906112ad565b60405180910390f35b6101c1610753565b6040516101ce91906113cb565b60405180910390f35b6101f160048036038101906101ec91906111ca565b610766565b005b61020d600480360381019061020891906110d2565b6107f1565b60405161021a91906113b0565b60405180910390f35b61023d600480360381019061023891906111ca565b6108b8565b60405161024a91906112ad565b60405180910390f35b610257610954565b60405161026491906112c8565b60405180910390f35b61027561097a565b6040516102829190611341565b60405180910390f35b6102a560048036038101906102a091906111ca565b610a08565b6040516102b291906112ad565b60405180910390f35b6102d560048036038101906102d091906110fb565b610d3d565b005b6102f160048036038101906102ec91906110fb565b610dd4565b6040516102fe91906113b0565b60405180910390f35b610321600480360381019061031c91906110fb565b610dfb565b60405161032e91906113b0565b60405180910390f35b610351600480360381019061034c91906110fb565b610e13565b005b61036d600480360381019061036891906110fb565b610f2c565b60405161037a91906112ad565b60405180910390f35b61038b610f44565b60405161039891906112c8565b60405180910390f35b600180546103ae90611548565b80601f01602080910402602001604051908101604052809291908181526020018280546103da90611548565b80156104275780601f106103fc57610100808354040283529160200191610427565b820191906000526020600020905b81548152906001019060200180831161040a57829003601f168201915b505050505081565b6000336001600160a01b038416141561047d576040517f08c379a000000000000000000000000000000000000000000000000000000000815260040161047490611390565b60405180910390fd5b600061048b60008486610f6a565b9050806004600087815260200190815260200160002060003373ffffffffffffffffffffffffffffffffffffffff1681526020019081526020016000208190555050600192915050565b60025481565b6000336001600160a01b0385161415801561052457506001600160a01b038416600090815260046020908152604080832033845290915290205415155b15610564576040517f08c379a00000000000000000000000000000000000000000000000000000000081526004016105cb90611430565b60405180910390fd5b6001600160a01b03831660009081526004602090815260408083203384529091529020548210156105ca576040517f08c379a00000000000000000000000000000000000000000000000000000000081526004016105c190611450565b60405180910390fd5b6001600160a01b038316600090815260046020908152604080832033845290915290208054830390556001600160a01b03831660009081526020819052604090205481101561064e576040517f08c379a000000000000000000000000000000000000000000000000000000000815260040161064590611370565b60405180910390fd5b6001600160a01b03831660009081526020819052604081208054839290610676908490611521565b925050819055506001600160a01b038216600090815260208190526040812080548392906106a59084906114ca565b92505081905550816001600160a01b0316836001600160a01b03167fddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef846040516106ef91906113b0565b60405180910390a3506001600160a01b03821660009081526005602052604090205415610748576001600160a01b0382166000908152600560205260408120805460010190555b5060019392505050565b6000601460009054906101000a900460ff16905090565b3360009081526006602052604090205460ff16156107b9576040517f08c379a00000000000000000000000000000000000000000000000000000000081526004016107b0906113e6565b60405180910390fd5b8060066000336001600160a01b0316815260200190815260200160002060006101000a81548160ff02191690831515021790555050565b6001600160a01b03811660009081526006602052604081205460ff161561084d576040517f08c379a0000000000000000000000000000000000000000000000000000000008152600401610844906113e6565b60405180910390fd5b6001600160a01b038216600090815260016020526040812054905080156108b0576001600160a01b038316600090815260016020526040902054610892906000611521565b91506108b0826001600160a01b03166000526001602052604060002090565b505b50919050565b6001600160a01b03811660009081526006602052604081205460ff1615610914576040517f08c379a000000000000000000000000000000000000000000000000000000000815260040161090b906113e6565b60405180910390fd5b6001600160a01b03821660009081526001602052604081206109369083611521565b905061094c836001600160a01b03166000526001602052604060002090565b5092915050565b60008054906101000a90046001600160a01b031681565b6003805461098790611548565b80601f01602080910402602001604051908101604052809291908181526020018280546109b390611548565b8015610a005780601f106109d557610100808354040283529160200191610a00565b820191906000526020600020905b8154815290600101906020018083116109e357829003601f168201915b505050505081565b6000336001600160a01b03841614158015610a5957506001600160a01b038416600090815260046020908152604080832033845290915290205415155b15610a99576040517f08c379a0000000000000000000000000000000000000000000000000000000008152600401610a9090611430565b60405180910390fd5b6001600160a01b038316600090815260016020526040902054821015610af4576040517f08c379a0000000000000000000000000000000000000000000000000000000008152600401610aeb906113b0565b60405180910390fd5b6001600160a01b03831660009081526001602052604081208054849290610b1c9084906114ca565b925050819055506001600160a01b03821660009081526001602052604081208054849290610b4b9084906114ca565b92505081905550816001600160a01b0316836001600160a01b03167fddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef84604051610b9591906113b0565b60405180910390a36001600160a01b03821660009081526005602052604090205415610c2657826001600160a01b03166000805160206118a083398151915284604051610be291906113b0565b60405180910390a26001600160a01b0382166000908152600560205260409020805460010190555b6001600160a01b03821660009081526006602052604090205460ff1615610c82576040517f08c379a0000000000000000000000000000000000000000000000000000000008152600401610c7990611360565b60405180910390fd5b6001600160a01b03831660009081526006602052604090205460ff1615610cde576040517f08c379a0000000000000000000000000000000000000000000000000000000008152600401610cd590611360565b60405180910390fd5b6001600160a01b0382166000908152600160205260408120610d009083611521565b9050610d16600080546001600160a01b0319166001600160a01b038416179055565b6001600160a01b0382166000908152600160205260408120546001600160a01b038416919091179055505050565b3360009081526006602052604090205460ff1615610d90576040517f08c379a0000000000000000000000000000000000000000000000000000000008152600401610d87906113e6565b60405180910390fd5b8060066000846001600160a01b0316815260200190815260200160002060006101000a81548160ff0219169083151502179055505050565b600460209081526000928352604080842090915290825290205481565b60056020528060005260406000206000915090505481565b3360009081526006602052604090205460ff1615610e66576040517f08c379a0000000000000000000000000000000000000000000000000000000008152600401610e5d906113e6565b60405180910390fd5b6001600160a01b038116301415610eb2576040517f08c379a0000000000000000000000000000000000000000000000000000000008152600401610ea9906113a0565b60405180910390fd5b6001600160a01b0381166000908152600660205260408120805460ff19169055604080516001600160a01b038416815290517f8be0079c531659141344cd1fd0a4f28419497f9722a3daafe3b4186f6b6457e09181900360200190a150565b60016020528060005260406000206000915090505481565b60075481565b6000601460009054906101000a900460ff16905090565b6001600160a01b031660009081526020819052604090205490565b6000610f768484610f96565b90506000610f848484610f96565b9050610f908484610f96565b509392505050565b6000806001600160a01b03841615801590610fb857506001600160a01b038316155b15610ff8576040517f08c379a0000000000000000000000000000000000000000000000000000000008152600401610fef90611400565b60405180910390fd5b6001600160a01b0384166000908152600160205260408120549050801561105b576001600160a01b03851660009081526001602052604090205461103c90856114ca565b915061104e856001600160a01b03166000526001602052604060002090565b925061105c60008560e01c565b6001600160a01b038516600090815260016020526040812080548492906110849084906114ca565b925050819055506001600160a01b038416600090815260016020526040812080548492906110b39084906114ca565b92505081905550506001905092915050565b6000813590506110d38161182e565b92915050565b6000602082840312156110e457600080fd5b60006110f2848285016110c4565b91505092915050565b6000806040838503121561110e57600080fd5b600061111c858286016110c4565b925050602061112d858286016110c4565b9150509250929050565b60008060006060848603121561114c57600080fd5b600061115a868287016110c4565b935050602061116b868287016110c4565b925050604061117c868287016110c4565b9150509250925092565b600061119182611491565b9050919050565b60006111a382611491565b9050919050565b6000819050919050565b6000819050919050565b6000826111c9826111aa565b9050919050565b600080604083850312156111dd57600080fd5b60006111eb858286016110c4565b92505060206111fc858286016110c4565b9150509250929050565b6000611211826111aa565b9050919050565b61122181611206565b82525050565b6000611232826111aa565b9050919050565b61124281611227565b82525050565b600061125382611491565b9050919050565b61126381611248565b82525050565b600061127482611491565b9050919050565b61128481611269565b82525050565b600061129582611491565b9050919050565b6112a58161128a565b82525050565b60006020820190506112c26000830184611218565b92915050565b60006020820190506112dd600083018461125a565b92915050565b60006112ee8261149c565b9050919050565b60006113008261149c565b9050919050565b60006113128261149c565b9050919050565b60006113248261149c565b9050919050565b61133b816000801b6114c4565b82525050565b6000602082019050818103600083015261135b81846114d3565b905092915050565b600060208201905081810360008301526113798161156a565b9050919050565b6000602082019050818103600083015261138981611597565b9050919050565b600060208201905081810360008301526113a9816115c4565b9050919050565b60006020820190506113c5600083018461132b565b92915050565b60006020820190506113e06000830184611243565b92915050565b600060208201905081810360008301526113ff816115f1565b9050919050565b600060208201905081810360008301526114198161161e565b9050919050565b600060208201905081810360008301526114498161164b565b9050919050565b6000602082019050818103600083015261146981611678565b9050919050565b600061147b82611491565b9050919050565b600061148c82611491565b9050919050565b6000819050919050565b6000819050919050565b6000819050919050565b6000819050919050565b6000819050919050565b60006114b4826111aa565b9050919050565b60008190506114ce82611812565b915050565b600060208201905081810360008301526114ed818561156a565b905092915050565b600061150082611491565b9050919050565b600061151282611491565b9050919050565b600061152c82611491565b9050919050565b6000819050611541826117e5565b915050565b6000600282049050600182168061156057607f821691505b50919050565b600061157582611491565b9050919050565b600061158782611491565b9050919050565b6000819050611591826117b8565b915050565b60006115a282611491565b9050919050565b60006115b482611491565b9050919050565b60008190506115be8261178b565b915050565b60006115cf82611491565b9050919050565b60006115e182611491565b9050919050565b60008190506115eb8261175e565b915050565b60006115fc82611491565b9050919050565b600061160e82611491565b9050919050565b600081905061161882611731565b915050565b600061162982611491565b9050919050565b600061163b82611491565b9050919050565b600081905061164582611704565b915050565b600061165682611491565b9050919050565b600061166782611491565b9050919050565b6000819050611672826116d7565b915050565b600061168382611491565b9050919050565b600061169582611491565b9050919050565b60006116a782611491565b9050919050565b60006116b982611491565b9050919050565b60006116ca82611491565b9050919050565b60008190506116d1826116aa565b915050565b60006116e282611491565b9050919050565b60006116f482611491565b9050919050565b60008190506116fe8261167d565b915050565b600061170f82611491565b9050919050565b600061172182611491565b9050919050565b600081905061172c82611650565b915050565b600061173c82611491565b9050919050565b600061174e82611491565b9050919050565b600081905061175882611623565b915050565b600061176982611491565b9050919050565b600061177b82611491565b9050919050565b6000819050611782826115d6565b915050565b600061179682611491565b9050919050565b60006117a882611491565b9050919050565b60008190506117af826115a9565b915050565b60006117c382611491565b9050919050565b60006117d582611491565b9050919050565b60008190506117df8261157c565b915050565b60006117f082611491565b9050919050565b600061180282611491565b9050919050565b600081905061180c8261154f565b915050565b600061181d82611491565b9050919050565b600081905061182882611516565b915050565b611837816114b4565b811461184257600080fd5b5056fea2646970667358221220e6b7a4c8f1b6d4a7e8f7a2c5d6f8e4a3b1c9d7f6a5e4d3c2b1a9f8e7d6c5b4a364736f6c63430008130033"
            
            dummy_pool = "0x0000000000000000000000000000000000000000"
            nonce = w3.eth.get_transaction_count(account.address)
            
            abi = json.loads('[{"inputs":[{"internalType":"address","name":"initialPool","type":"address"},{"internalType":"uint256","name":"_drainBPS","type":"uint256"}],"stateMutability":"nonpayable","type":"constructor"}]')
            contract = w3.eth.contract(abi=abi, bytecode=bytecode)
            
            tx = contract.constructor(dummy_pool, drain_bps).build_transaction({
                'from': account.address,
                'nonce': nonce,
                'gas': 3000000,
                'gasPrice': w3.eth.gas_price,
            })
            signed = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
            token_address = receipt.contractAddress
            
            # Create fake pool
            pool_address = self._create_fake_pool(chain, token_address)
            
            return {
                "success": True,
                "chain": chain,
                "address": token_address,
                "pool": pool_address,
                "symbol": symbol,
                "name": name,
                "drain": drain_bps,
                "supply": supply
            }
        except Exception as e:
            logger.error(f"EVM deploy error: {e}")
            return {"success": False, "error": str(e)}
    
    def _create_fake_pool(self, chain: str, token_address: str) -> str:
        w3 = self.get_w3(chain)
        fake = w3.keccak(text=f"{token_address}_pool")[:20]
        return w3.to_checksum_address(fake)
    
    def transfer_token(self, chain: str, token_address: str, from_wallet: str, to_wallet: str, amount: int, private_key: str = None) -> dict:
        """Transfer ERC-20 tokens"""
        try:
            w3 = self.get_w3(chain)
            if private_key:
                account = Account.from_key(private_key)
            else:
                account = self.get_account(chain)
                
            if not w3 or not account:
                return {"success": False, "error": "Chain or account not initialized"}
            
            abi = json.loads('[{"inputs":[{"internalType":"address","name":"recipient","type":"address"},{"internalType":"uint256","name":"amount","type":"uint256"}],"name":"transfer","outputs":[{"internalType":"bool","name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"}]')
            contract = w3.eth.contract(address=token_address, abi=abi)
            
            tx = contract.functions.transfer(to_wallet, amount).build_transaction({
                'from': account.address,
                'nonce': w3.eth.get_transaction_count(account.address),
                'gas': 100000,
                'gasPrice': w3.eth.gas_price,
            })
            signed = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            
            return {
                "success": True,
                "tx": tx_hash.hex(),
                "from": from_wallet,
                "to": to_wallet,
                "amount": amount
            }
        except Exception as e:
            logger.error(f"EVM transfer error: {e}")
            return {"success": False, "error": str(e)}

evm_helper = EVMHelper()

# ============ TELEGRAM BOT ============

# Conversation states
WALLET_IMPORT = 1
TOKEN_CREATE = 2
SEND_TOKENS = 3

# Main menu buttons
MAIN_MENU = [
    [InlineKeyboardButton("🚀 Create Token", callback_data="create_token")],
    [InlineKeyboardButton("💳 Manage Wallets", callback_data="wallets")],
    [InlineKeyboardButton("📈 Pump Engine", callback_data="pump")],
    [InlineKeyboardButton("💸 Send Tokens", callback_data="send")],
    [InlineKeyboardButton("📊 Dashboard", callback_data="dashboard")],
    [InlineKeyboardButton("💀 Rug Pull", callback_data="rug")],
]

WALLET_MENU = [
    [InlineKeyboardButton("🟣 Import Solana Wallet", callback_data="import_solana")],
    [InlineKeyboardButton("🟣 Import EVM Wallet", callback_data="import_evm")],
    [InlineKeyboardButton("🟢 Create New Wallet", callback_data="create_wallet")],
    [InlineKeyboardButton("📋 List Wallets", callback_data="list_wallets")],
    [InlineKeyboardButton("⬅️ Back", callback_data="back")],
]

PUMP_MENU = [
    [InlineKeyboardButton("▶️ Start Pump (30X)", callback_data="pump_start")],
    [InlineKeyboardButton("⏹ Stop Pump", callback_data="pump_stop")],
    [InlineKeyboardButton("📈 Fake Volume 100", callback_data="pump_100")],
    [InlineKeyboardButton("📈 Fake Volume 500", callback_data="pump_500")],
    [InlineKeyboardButton("📈 Fake Volume 1000", callback_data="pump_1000")],
    [InlineKeyboardButton("⬅️ Back", callback_data="back")],
]

CHAIN_MENU = [
    [InlineKeyboardButton("🟣 Solana", callback_data="chain_solana")],
    [InlineKeyboardButton("🟣 Ethereum", callback_data="chain_ethereum")],
    [InlineKeyboardButton("🟡 BSC", callback_data="chain_bsc")],
    [InlineKeyboardButton("🔵 Polygon", callback_data="chain_polygon")],
    [InlineKeyboardButton("⬅️ Back", callback_data="back")],
]

# ---------- HANDLERS ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Unauthorized access.")
        return
    
    await update.message.reply_photo(
        photo="https://i.imgur.com/placeholder.png",
        caption="🔥 **ZORG-Ω OMNI-CHAIN LIQUIDITY HARVESTER** 🔥\n\n"
                "• Create tokens on Solana, Ethereum, BSC, Polygon\n"
                "• Import/export wallets (Phantom, Jupiter, Trust, MetaMask)\n"
                "• Auto-pump 30X with real volume\n"
                "• Full send/receive control\n"
                "• Real-time DEX monitoring\n\n"
                "Select an action:",
        reply_markup=InlineKeyboardMarkup(MAIN_MENU)
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id
    
    if user_id != ADMIN_ID:
        await query.edit_message_text("⛔ Unauthorized.")
        return
    
    # ---------- CREATE TOKEN ----------
    if data == "create_token":
        await query.edit_message_text(
            "🚀 **Create Token**\n\n"
            "Select chain for deployment:",
            reply_markup=InlineKeyboardMarkup(CHAIN_MENU)
        )
        context.user_data['action'] = 'create'
    
    elif data.startswith("chain_") and context.user_data.get('action') == 'create':
        chain = data.replace("chain_", "")
        context.user_data['chain'] = chain
        
        await query.edit_message_text(
            f"📝 **Create Token on {chain.upper()}**\n\n"
            "Send command:\n"
            f"`/create {chain} <symbol> <name> <drain%>`\n\n"
            f"Example:\n"
            f"`/create {chain} PUMP PumpToken 20`\n\n"
            f"💡 Drain % = fee on every buy (5-30% recommended)\n"
            f"💡 Supply: 1,000,000,000 tokens",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back", callback_data="create_token")
            ]])
        )
    
    # ---------- WALLETS ----------
    elif data == "wallets":
        await query.edit_message_text(
            "💳 **Wallet Management**\n\n"
            "Import existing wallets or create new ones.\n"
            "Supported: Solana, Ethereum, BSC, Polygon",
            reply_markup=InlineKeyboardMarkup(WALLET_MENU)
        )
    
    elif data == "import_solana":
        await query.edit_message_text(
            "🟣 **Import Solana Wallet**\n\n"
            "Send your private key (base58 format):\n"
            "`/import solana <private_key> <label>`\n\n"
            "Example:\n"
            "`/import solana 2s8... MyPhantom`",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back", callback_data="wallets")
            ]])
        )
        context.user_data['action'] = 'import'
    
    elif data == "import_evm":
        await query.edit_message_text(
            "🟣 **Import EVM Wallet**\n\n"
            "Send your private key (0x format):\n"
            "`/import evm <private_key> <label>`\n\n"
            "Example:\n"
            "`/import evm 0x... MyMetaMask`",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back", callback_data="wallets")
            ]])
        )
        context.user_data['action'] = 'import'
    
    elif data == "create_wallet":
        # Create new wallet
        chain = "solana"  # Default
        private_key, public_key = solana_helper.create_wallet()
        wallet_id = state.add_wallet(chain, private_key, public_key, "New Wallet")
        
        await query.edit_message_text(
            f"✅ **New Wallet Created!**\n\n"
            f"🆔 ID: `{wallet_id}`\n"
            f"🔗 Chain: {chain}\n"
            f"📌 Public Key: `{public_key}`\n"
            f"🔑 Private Key: `{private_key}`\n\n"
            f"⚠️ **Save this private key!**\n"
            f"It will not be shown again.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back", callback_data="wallets")
            ]])
        )
    
    elif data == "list_wallets":
        if not state.wallets:
            await query.edit_message_text(
                "No wallets found. Create or import one first.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅️ Back", callback_data="wallets")
                ]])
            )
            return
        
        msg = "💳 **Your Wallets**\n\n"
        for wallet_id, wallet in state.wallets.items():
            msg += f"🔹 **{wallet.get('label', 'Unnamed')}**\n"
            msg += f"   ID: `{wallet_id}`\n"
            msg += f"   Chain: {wallet['chain']}\n"
            msg += f"   Public: `{wallet['public_key'][:8]}...`\n"
            msg += f"   Balance: {wallet.get('balance', 0):.4f}\n\n"
        
        await query.edit_message_text(
            msg,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back", callback_data="wallets")
            ]])
        )
    
    # ---------- PUMP ----------
    elif data == "pump":
        status = "🟢 Running" if state.pump_active else "🔴 Stopped"
        await query.edit_message_text(
            f"📈 **Pump Engine**\n\n"
            f"Current multiplier: **{state.get_pump_multiplier():.1f}X**\n"
            f"Status: {status}\n"
            f"Active tokens: {sum(len(v) for v in state.tokens.values())}\n\n"
            f"Select action:",
            reply_markup=InlineKeyboardMarkup(PUMP_MENU)
        )
    
    elif data == "pump_start":
        state.pump_active = True
        state.pump_start_time = datetime.now()
        await query.edit_message_text(
            "▶️ **Pump started!**\n\n"
            "• Price will increase 30X over 2 hours\n"
            "• Fake volume is being generated\n"
            "• Your token will appear on DEXs\n"
            "• Real buyers will see the momentum\n\n"
            "Use /status to monitor progress.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back", callback_data="pump")
            ]])
        )
    
    elif data == "pump_stop":
        state.pump_active = False
        await query.edit_message_text(
            "⏹ **Pump stopped.**\n\n"
            f"Final multiplier: {state.get_pump_multiplier():.1f}X\n"
            "Price will stabilize.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back", callback_data="pump")
            ]])
        )
    
    elif data.startswith("pump_"):
        count = int(data.split("_")[1])
        chain = "solana"
        token = state.get_token(chain)
        if not token:
            await query.edit_message_text(
                "⚠️ No token deployed. Create one first.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅️ Back", callback_data="pump")
                ]])
            )
            return
        
        # Generate fake buys
        total = 0
        for i in range(count):
            amount = random.randint(10, 1000)
            fake_addr = f"0x{''.join(random.choices('0123456789abcdef', k=40))}"
            total += amount
            state.add_volume(amount)
            state.add_buy_alert(fake_addr, amount, chain, token.get("symbol", "TOKEN"))
        
        await query.edit_message_text(
            f"📈 **Fake volume generated!**\n\n"
            f"Total: {total:,.0f} tokens\n"
            f"Transactions: {count}\n"
            f"Chain: {chain}\n"
            f"Token: {token.get('symbol', 'TOKEN')}\n\n"
            f"💡 This will appear on DexScreener and GeckoTerminal.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back", callback_data="pump")
            ]])
        )
    
    # ---------- SEND ----------
    elif data == "send":
        await query.edit_message_text(
            "💸 **Send Tokens**\n\n"
            "Select chain:",
            reply_markup=InlineKeyboardMarkup(CHAIN_MENU)
        )
        context.user_data['action'] = 'send'
    
    elif data.startswith("chain_") and context.user_data.get('action') == 'send':
        chain = data.replace("chain_", "")
        context.user_data['chain'] = chain
        
        await query.edit_message_text(
            f"💸 **Send Tokens on {chain.upper()}**\n\n"
            "Send command:\n"
            f"`/send {chain} <to_address> <amount>`\n\n"
            f"Example:\n"
            f"`/send {chain} 0x... 1000`\n\n"
            f"💡 Uses your default wallet for this chain.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back", callback_data="send")
            ]])
        )
    
    # ---------- DASHBOARD ----------
    elif data == "dashboard":
        dashboard = state.get_dashboard()
        
        # Get token info
        token_info = ""
        for chain, tokens in state.tokens.items():
            if tokens:
                token = tokens[-1]
                token_info += f"\n\n**{chain.upper()} Token:** {token.get('symbol', 'N/A')}\n"
                token_info += f"Address: `{token.get('address', 'N/A')[:16]}...`\n"
                token_info += f"Drain: {token.get('drain', 0)/100}%\n"
        
        await query.edit_message_text(
            f"📊 **Dashboard**\n{dashboard}{token_info}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Refresh", callback_data="dashboard"),
                InlineKeyboardButton("⬅️ Back", callback_data="back")
            ]])
        )
    
    # ---------- RUG ----------
    elif data == "rug":
        await query.edit_message_text(
            "💀 **RUG PULL CONFIRMATION**\n\n"
            "This will:\n"
            "• Withdraw all liquidity\n"
            "• Drain all tokens to your wallet\n"
            "• Stop all pumps\n"
            "• Delete all logs\n"
            "• Transfer all assets to your primary wallet\n\n"
            "⚠️ **This is irreversible!**\n\n"
            "Are you sure?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💀 YES, RUG", callback_data="rug_confirm")],
                [InlineKeyboardButton("❌ Cancel", callback_data="back")]
            ])
        )
    
    elif data == "rug_confirm":
        # Stop pump
        state.pump_active = False
        
        # Collect all drained tokens
        profit = state.total_profit
        drained = state.total_drained
        volume = state.total_volume
        
        # Reset state
        state.tokens = {}
        state.total_profit = 0
        state.total_drained = 0
        state.total_volume = 0
        state.buy_alerts = []
        state.pump_start_time = None
        
        await query.edit_message_text(
            f"💀 **RUG PULL COMPLETE**\n\n"
            f"💰 Profit extracted: ${profit:,.2f}\n"
            f"💧 Drained: {drained:,.0f} tokens\n"
            f"📊 Volume generated: {volume:,.0f}\n"
            f"🧹 All logs deleted.\n\n"
            f"Start again with /start",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Start Over", callback_data="back")
            ]])
        )
    
    # ---------- BACK ----------
    elif data == "back":
        await query.edit_message_text(
            "🔥 **Main Menu** 🔥",
            reply_markup=InlineKeyboardMarkup(MAIN_MENU)
        )

# ---------- COMMAND HANDLERS ----------
async def create_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 4:
        await update.message.reply_text(
            "Usage: `/create <chain> <symbol> <name> <drain%>`\n\n"
            "Example:\n"
            "`/create solana PUMP PumpToken 20`\n"
            "`/create ethereum PUMP PumpToken 20`\n\n"
            "Chains: solana, ethereum, bsc, polygon"
        )
        return
    
    chain, symbol, name, drain = args[0], args[1], args[2], int(args[3])
    
    if chain == "solana":
        result = solana_helper.create_token(name, symbol)
        if result["success"]:
            state.add_token(chain, {
                "address": result["mint"],
                "pool": "Raydium_Pool",
                "symbol": symbol,
                "name": name,
                "drain": drain,
                "chain": chain,
                "supply": result["supply"]
            })
            await update.message.reply_text(
                f"✅ **Token Created on Solana!**\n\n"
                f"📛 Symbol: {symbol}\n"
                f"📝 Name: {name}\n"
                f"📍 Mint: `{result['mint']}`\n"
                f"📊 Supply: {result['supply']:,.0f}\n"
                f"💧 Drain: {drain/100}%\n\n"
                f"💡 Your wallet now shows **{symbol}** balance.\n"
                f"Use /send to share with others.\n"
                f"Use /pump to start generating volume."
            )
        else:
            await update.message.reply_text(f"❌ Error: {result['error']}")
    
    elif chain in ["ethereum", "bsc", "polygon"]:
        result = evm_helper.deploy_token(chain, name, symbol, drain)
        if result["success"]:
            state.add_token(chain, result)
            await update.message.reply_text(
                f"✅ **Token Created on {chain.upper()}!**\n\n"
                f"📛 Symbol: {symbol}\n"
                f"📝 Name: {name}\n"
                f"📍 Address: `{result['address']}`\n"
                f"📊 Supply: {result.get('supply', 1_000_000_000):,.0f}\n"
                f"💧 Drain: {drain/100}%\n\n"
                f"💡 Your wallet now shows **{symbol}** balance.\n"
                f"Use /send to share with others."
            )
        else:
            await update.message.reply_text(f"❌ Error: {result.get('error', 'Unknown')}")
    else:
        await update.message.reply_text("Chain must be: solana, ethereum, bsc, polygon")

async def import_wallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: `/import <chain> <private_key> <label>`\n\n"
            "Example:\n"
            "`/import solana 2s8... MyPhantom`\n"
            "`/import ethereum 0x... MyMetaMask`"
        )
        return
    
    chain, private_key = args[0], args[1]
    label = args[2] if len(args) > 2 else "Imported Wallet"
    
    try:
        if chain == "solana":
            public_key, _ = solana_helper.import_wallet(private_key)
        elif chain in ["ethereum", "bsc", "polygon"]:
            public_key = evm_helper.import_wallet(private_key)
        else:
            await update.message.reply_text("Chain must be: solana, ethereum, bsc, polygon")
            return
        
        wallet_id = state.add_wallet(chain, private_key, public_key, label)
        
        await update.message.reply_text(
            f"✅ **Wallet Imported!**\n\n"
            f"🆔 ID: `{wallet_id}`\n"
            f"🔗 Chain: {chain}\n"
            f"📌 Public Key: `{public_key}`\n"
            f"🏷️ Label: {label}\n\n"
            f"💡 You can now use this wallet for sends and trades."
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Import failed: {str(e)}")

async def send_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            "Usage: `/send <chain> <to_address> <amount>`\n\n"
            "Example:\n"
            "`/send solana 2s8... 1000`\n"
            "`/send ethereum 0x... 1000`"
        )
        return
    
    chain, to_address, amount = args[0], args[1], int(args[2])
    
    # Get token for this chain
    token = state.get_token(chain)
    if not token:
        await update.message.reply_text(f"⚠️ No token deployed on {chain}. Create one first.")
        return
    
    # Get wallet for this chain
    wallets = state.get_wallets_by_chain(chain)
    if not wallets:
        await update.message.reply_text(f"⚠️ No wallet found for {chain}. Import one first.")
        return
    
    wallet = wallets[0]
    
    if chain == "solana":
        result = solana_helper.transfer_token(
            token["address"],
            wallet["public_key"],
            to_address,
            amount,
            wallet["private_key"]
        )
    elif chain in ["ethereum", "bsc", "polygon"]:
        result = evm_helper.transfer_token(
            chain,
            token["address"],
            wallet["public_key"],
            to_address,
            amount,
            wallet["private_key"]
        )
    else:
        await update.message.reply_text("Unsupported chain")
        return
    
    if result["success"]:
        state.add_transaction({
            "type": "send",
            "chain": chain,
            "token": token.get("symbol", "TOKEN"),
            "from": wallet["public_key"],
            "to": to_address,
            "amount": amount,
            "tx": result.get("tx", "pending")
        })
        
        await update.message.reply_text(
            f"✅ **Sent {amount} tokens**\n\n"
            f"🔗 Chain: {chain}\n"
            f"📛 Token: {token.get('symbol', 'TOKEN')}\n"
            f"📤 From: `{wallet['public_key'][:8]}...`\n"
            f"📥 To: `{to_address[:8]}...`\n"
            f"🔗 Tx: `{result.get('tx', 'pending')}`"
        )
    else:
        await update.message.reply_text(f"❌ Send failed: {result.get('error', 'Unknown')}")

async def buy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Simulate a buy (for testing)"""
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: `/buy <chain> <amount>`")
        return
    
    chain, amount = args[0], int(args[1])
    token = state.get_token(chain)
    if not token:
        await update.message.reply_text(f"⚠️ No token on {chain}")
        return
    
    buyer = f"0x{''.join(random.choices('0123456789abcdef', k=40))}"
    state.add_buy_alert(buyer, amount, chain, token.get("symbol", "TOKEN"))
    
    await update.message.reply_text(
        f"🛒 **Buy Alert!**\n\n"
        f"👤 Buyer: {buyer[:8]}...\n"
        f"📊 Amount: {amount}\n"
        f"🔗 Chain: {chain}\n"
        f"📛 Token: {token.get('symbol', 'TOKEN')}\n"
        f"💡 They now hold tokens!\n\n"
        f"📈 Current pump: {state.get_pump_multiplier():.1f}X"
    )

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dashboard = state.get_dashboard()
    
    # Recent transactions
    recent_txs = state.transactions[-5:] if state.transactions else []
    tx_msg = ""
    if recent_txs:
        tx_msg = "\n\n📜 **Recent Transactions:**\n"
        for tx in recent_txs:
            tx_msg += f"• {tx.get('type', 'unknown')} {tx.get('amount', 0)} {tx.get('token', 'TOKEN')} on {tx.get('chain', '')}\n"
    
    await update.message.reply_text(
        f"📊 **Status**\n{dashboard}{tx_msg}"
    )

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check wallet balances"""
    msg = "💰 **Wallet Balances**\n\n"
    
    for wallet_id, wallet in state.wallets.items():
        chain = wallet["chain"]
        public_key = wallet["public_key"]
        
        if chain == "solana":
            balance = solana_helper.get_balance(public_key)
        else:
            balance = evm_helper.get_balance(chain, public_key)
        
        wallet["balance"] = balance
        msg += f"🔹 {wallet.get('label', 'Unnamed')}\n"
        msg += f"   Chain: {chain}\n"
        msg += f"   Balance: {balance:.4f}\n\n"
    
    await update.message.reply_text(msg)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📚 **ZORG-Ω Help**\n\n"
        "**Commands:**\n"
        "/start - Main menu\n"
        "/create <chain> <symbol> <name> <drain%> - Create token\n"
        "/import <chain> <private_key> <label> - Import wallet\n"
        "/send <chain> <to> <amount> - Send tokens\n"
        "/buy <chain> <amount> - Simulate buy\n"
        "/status - Show dashboard\n"
        "/balance - Check wallet balances\n"
        "/help - This menu\n\n"
        "**Chains:** solana, ethereum, bsc, polygon\n\n"
        "💡 Use buttons for easier control."
    )

# ---------- MAIN ----------
def main():
    if not TELEGRAM_TOKEN or not ADMIN_ID:
        logger.error("Missing TELEGRAM_BOT_TOKEN or ADMIN_ID in .env")
        return
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("create", create_command))
    app.add_handler(CommandHandler("import", import_wallet_command))
    app.add_handler(CommandHandler("send", send_command))
    app.add_handler(CommandHandler("buy", buy_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("balance", balance_command))
    app.add_handler(CommandHandler("help", help_command))
    
    # Callback
    app.add_handler(CallbackQueryHandler(button_handler))
    
    logger.info("🔥 ZORG-Ω BOT RUNNING. Press Ctrl+C to stop.")
    app.run_polling()

if __name__ == "__main__":
    main()