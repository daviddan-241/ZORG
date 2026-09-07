"""Phantom Omni — Real Blockchain Connections

This module handles:
- BSC (Binance Smart Chain) via Web3.py
- ETH (Ethereum) via Web3.py  
- SOL (Solana) via Solana.py
- Contract deployment
- Balance queries
- Transaction signing with seed wallet
"""

from __future__ import annotations

import logging
import time
import json
import secrets
from typing import Optional, Dict, Any, List, Tuple
from decimal import Decimal
from web3 import Web3
from web3.middleware import geth_poa_middleware
from eth_account import Account
from solana.rpc.api import Client as SolanaClient
from solana.keypair import Keypair
from solana.publickey import PublicKey
from solders.transaction import Transaction
from solders.system_program import TransferParams, transfer
import base58
import bip_utils
import os

log = logging.getLogger("solo-metro.phantom.chains")

# ================================================================
# CHAIN CONFIGURATION
# ================================================================

CHAIN_CONFIG = {
    "BSC": {
        "name": "Binance Smart Chain",
        "native": "BNB",
        "chain_id": 56,
        "decimals": 18,
        "rpc": "https://bsc-dataseed.binance.org/",
        "rpc_backup": "https://bsc-dataseed1.defibit.io/",
        "explorer": "https://bscscan.com",
        "dex": "PancakeSwap",
        "router": "0x10ED43C718714eb63d5aA57B78B54704E256024E",
        "symbol": "BNB",
        "enabled": True
    },
    "ETH": {
        "name": "Ethereum",
        "native": "ETH",
        "chain_id": 1,
        "decimals": 18,
        "rpc": "https://eth.llamarpc.com",
        "rpc_backup": "https://rpc.ankr.com/eth",
        "explorer": "https://etherscan.io",
        "dex": "Uniswap",
        "router": "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
        "symbol": "ETH",
        "enabled": True
    },
    "SOL": {
        "name": "Solana",
        "native": "SOL",
        "chain_id": 0,
        "decimals": 9,
        "rpc": "https://api.mainnet-beta.solana.com",
        "rpc_backup": "https://solana-api.projectserum.com",
        "explorer": "https://solscan.io",
        "dex": "Raydium",
        "symbol": "SOL",
        "enabled": False
    }
}


# ================================================================
# SEED WALLET DERIVATION
# ================================================================

def derive_wallets_from_seed(seed_phrase: str) -> Dict[str, Dict[str, Any]]:
    """
    Derive all wallets from a seed phrase.
    Returns wallets for BSC, ETH, SOL with main and burner addresses.
    """
    wallets = {}
    
    # Clean seed phrase
    seed_phrase = seed_phrase.strip().lower()
    
    # BSC / ETH (same derivation path)
    eth_seed = bip_utils.Bip39SeedGenerator(seed_phrase).Generate()
    bip44_ctx = bip_utils.Bip44.FromSeed(eth_seed, bip_utils.Bip44Coins.ETHEREUM)
    
    # Main wallet (m/44'/60'/0'/0/0)
    main_ctx = bip44_ctx.Purpose().Coin().Account(0).Change(0).AddressIndex(0)
    main_private_key = main_ctx.PrivateKey().Raw().ToHex()
    main_address = main_ctx.PublicKey().ToAddress()
    
    # Burner wallet (m/44'/60'/0'/0/1)
    burner_ctx = bip44_ctx.Purpose().Coin().Account(0).Change(0).AddressIndex(1)
    burner_private_key = burner_ctx.PrivateKey().Raw().ToHex()
    burner_address = burner_ctx.PublicKey().ToAddress()
    
    wallets["ETH"] = {
        "main": {
            "address": main_address,
            "private_key": main_private_key,
            "public_key": main_ctx.PublicKey().Raw().ToHex()
        },
        "burner": {
            "address": burner_address,
            "private_key": burner_private_key,
            "public_key": burner_ctx.PublicKey().Raw().ToHex()
        }
    }
    
    # BSC (same as ETH with different chain_id)
    wallets["BSC"] = {
        "main": {
            "address": main_address,
            "private_key": main_private_key,
            "public_key": main_ctx.PublicKey().Raw().ToHex()
        },
        "burner": {
            "address": burner_address,
            "private_key": burner_private_key,
            "public_key": burner_ctx.PublicKey().Raw().ToHex()
        }
    }
    
    # SOL (derivation path: m/44'/501'/0'/0')
    try:
        sol_seed = bip_utils.Bip39SeedGenerator(seed_phrase).Generate()
        sol_bip44 = bip_utils.Bip44.FromSeed(sol_seed, bip_utils.Bip44Coins.SOLANA)
        
        # Main wallet
        sol_main_ctx = sol_bip44.Purpose().Coin().Account(0).Change(0).AddressIndex(0)
        sol_main_private = sol_main_ctx.PrivateKey().Raw().ToBytes()
        sol_main_keypair = Keypair.from_secret_key(sol_main_private[:32])
        
        # Burner wallet
        sol_burner_ctx = sol_bip44.Purpose().Coin().Account(0).Change(0).AddressIndex(1)
        sol_burner_private = sol_burner_ctx.PrivateKey().Raw().ToBytes()
        sol_burner_keypair = Keypair.from_secret_key(sol_burner_private[:32])
        
        wallets["SOL"] = {
            "main": {
                "address": str(sol_main_keypair.public_key),
                "private_key": base58.b58encode(sol_main_private[:32]).decode(),
                "keypair": sol_main_keypair
            },
            "burner": {
                "address": str(sol_burner_keypair.public_key),
                "private_key": base58.b58encode(sol_burner_private[:32]).decode(),
                "keypair": sol_burner_keypair
            }
        }
    except Exception as e:
        log.warning(f"SOL derivation failed: {e}")
        wallets["SOL"] = {
            "main": {"address": "", "private_key": ""},
            "burner": {"address": "", "private_key": ""}
        }
    
    return wallets


# ================================================================
# CHAIN HANDLER BASE
# ================================================================

class ChainHandler:
    """Base class for chain handlers."""
    
    def __init__(self, chain: str, seed_phrase: str = None):
        self.chain = chain
        self.config = CHAIN_CONFIG.get(chain, {})
        self.seed_phrase = seed_phrase
        self.wallets = None
        self.web3 = None
        self.solana_client = None
        self._connected = False
        
        if seed_phrase:
            self.wallets = derive_wallets_from_seed(seed_phrase)
    
    def is_connected(self) -> bool:
        return self._connected
    
    def get_main_address(self) -> str:
        if not self.wallets:
            return ""
        return self.wallets.get(self.chain, {}).get("main", {}).get("address", "")
    
    def get_burner_address(self) -> str:
        if not self.wallets:
            return ""
        return self.wallets.get(self.chain, {}).get("burner", {}).get("address", "")
    
    def get_main_private_key(self) -> str:
        if not self.wallets:
            return ""
        return self.wallets.get(self.chain, {}).get("main", {}).get("private_key", "")
    
    def get_burner_private_key(self) -> str:
        if not self.wallets:
            return ""
        return self.wallets.get(self.chain, {}).get("burner", {}).get("private_key", "")
    
    async def connect(self) -> bool:
        raise NotImplementedError
    
    async def get_balance(self, address: str) -> float:
        raise NotImplementedError
    
    async def deploy_contract(self, name: str, symbol: str, supply: int, threshold: float, burner: str, drain_pct: int) -> Dict[str, Any]:
        raise NotImplementedError
    
    async def get_transaction(self, tx_hash: str) -> Dict[str, Any]:
        raise NotImplementedError
    
    async def send_transaction(self, from_address: str, to_address: str, amount: float, private_key: str = None) -> Dict[str, Any]:
        raise NotImplementedError


# ================================================================
# EVM CHAIN HANDLER (BSC, ETH)
# ================================================================

class EVMHandler(ChainHandler):
    """EVM chain handler for BSC and ETH."""
    
    def __init__(self, chain: str, seed_phrase: str = None):
        super().__init__(chain, seed_phrase)
        self._account = None
    
    async def connect(self) -> bool:
        """Connect to the EVM chain."""
        try:
            rpc = self.config.get("rpc", "")
            if not rpc:
                return False
            
            self.web3 = Web3(Web3.HTTPProvider(rpc))
            
            # Add POA middleware for BSC
            if self.chain == "BSC":
                self.web3.middleware_onion.inject(geth_poa_middleware, layer=0)
            
            if not self.web3.is_connected():
                log.warning(f"{self.chain} not connected, trying backup...")
                backup = self.config.get("rpc_backup", "")
                if backup:
                    self.web3 = Web3(Web3.HTTPProvider(backup))
                    if self.web3.is_connected():
                        self._connected = True
                        log.info(f"{self.chain} connected via backup")
                        return True
                return False
            
            self._connected = True
            log.info(f"{self.chain} connected")
            return True
            
        except Exception as e:
            log.error(f"{self.chain} connection failed: {e}")
            self._connected = False
            return False
    
    async def get_balance(self, address: str) -> float:
        """Get native balance in wei, convert to decimal."""
        if not self.web3 or not self._connected:
            await self.connect()
        
        try:
            wei = self.web3.eth.get_balance(Web3.to_checksum_address(address))
            decimals = self.config.get("decimals", 18)
            return wei / (10 ** decimals)
        except Exception as e:
            log.error(f"Balance query failed: {e}")
            return 0.0
    
    async def deploy_contract(
        self,
        name: str,
        symbol: str,
        supply: int,
        threshold: float,
        burner: str,
        drain_pct: int = 90,
        gas_limit: int = 3000000
    ) -> Dict[str, Any]:
        """
        Deploy the PhantomToken contract.
        Returns contract address and transaction details.
        """
        if not self.web3 or not self._connected:
            await self.connect()
        
        if not self.seed_phrase:
            raise ValueError("Seed phrase required for deployment")
        
        # Get the contract ABI and bytecode
        contract_abi, contract_bytecode = self._get_contract_abi_bytecode()
        
        # Get main wallet private key
        private_key = self.get_main_private_key()
        if not private_key:
            raise ValueError("No private key available")
        
        # Get deployer address
        deployer_address = self.get_main_address()
        
        # Get router address
        router = self.config.get("router", "")
        
        # Prepare constructor arguments
        decimals = self.config.get("decimals", 18)
        supply_wei = supply * (10 ** decimals)
        threshold_wei = int(threshold * (10 ** decimals))
        
        # Create contract instance
        contract = self.web3.eth.contract(
            abi=contract_abi,
            bytecode=contract_bytecode
        )
        
        # Build constructor transaction
        construct_txn = contract.constructor(
            name,
            symbol,
            supply_wei,
            threshold_wei,
            Web3.to_checksum_address(router),
            Web3.to_checksum_address(burner),
            drain_pct
        ).build_transaction({
            "from": Web3.to_checksum_address(deployer_address),
            "nonce": self.web3.eth.get_transaction_count(Web3.to_checksum_address(deployer_address)),
            "gas": gas_limit,
            "gasPrice": self.web3.eth.gas_price,
            "chainId": self.config.get("chain_id", 56)
        })
        
        # Sign transaction
        signed_txn = self.web3.eth.account.sign_transaction(construct_txn, private_key)
        
        # Send transaction
        tx_hash = self.web3.eth.send_raw_transaction(signed_txn.raw_transaction)
        
        # Wait for receipt
        receipt = self.web3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        
        if receipt.status != 1:
            raise ValueError(f"Deployment failed: {receipt}")
        
        contract_address = receipt.contractAddress
        
        return {
            "address": contract_address,
            "tx_hash": tx_hash.hex(),
            "receipt": receipt,
            "explorer_url": f"{self.config.get('explorer', '')}/address/{contract_address}",
            "chain": self.chain
        }
    
    async def get_transaction(self, tx_hash: str) -> Dict[str, Any]:
        """Get transaction details."""
        if not self.web3 or not self._connected:
            await self.connect()
        
        try:
            tx = self.web3.eth.get_transaction(tx_hash)
            receipt = self.web3.eth.get_transaction_receipt(tx_hash)
            
            return {
                "hash": tx_hash,
                "from": tx.get("from"),
                "to": tx.get("to"),
                "value": tx.get("value", 0) / (10 ** self.config.get("decimals", 18)),
                "gas_used": receipt.get("gasUsed", 0),
                "status": receipt.get("status", 0),
                "block_number": receipt.get("blockNumber", 0),
                "explorer_url": f"{self.config.get('explorer', '')}/tx/{tx_hash}"
            }
        except Exception as e:
            log.error(f"Transaction query failed: {e}")
            return {"error": str(e)}
    
    async def send_transaction(
        self,
        from_address: str,
        to_address: str,
        amount: float,
        private_key: str = None,
        gas_limit: int = 21000
    ) -> Dict[str, Any]:
        """Send native currency to an address."""
        if not self.web3 or not self._connected:
            await self.connect()
        
        # Determine which wallet to use
        if not private_key:
            if from_address.lower() == self.get_main_address().lower():
                private_key = self.get_main_private_key()
            elif from_address.lower() == self.get_burner_address().lower():
                private_key = self.get_burner_private_key()
            else:
                raise ValueError("Unknown wallet address")
        
        if not private_key:
            raise ValueError("No private key available")
        
        decimals = self.config.get("decimals", 18)
        amount_wei = int(amount * (10 ** decimals))
        
        # Build transaction
        tx = {
            "from": Web3.to_checksum_address(from_address),
            "to": Web3.to_checksum_address(to_address),
            "value": amount_wei,
            "gas": gas_limit,
            "gasPrice": self.web3.eth.gas_price,
            "nonce": self.web3.eth.get_transaction_count(Web3.to_checksum_address(from_address)),
            "chainId": self.config.get("chain_id", 56)
        }
        
        # Sign and send
        signed_txn = self.web3.eth.account.sign_transaction(tx, private_key)
        tx_hash = self.web3.eth.send_raw_transaction(signed_txn.raw_transaction)
        
        return {
            "tx_hash": tx_hash.hex(),
            "from": from_address,
            "to": to_address,
            "amount": amount,
            "chain": self.chain,
            "explorer_url": f"{self.config.get('explorer', '')}/tx/{tx_hash.hex()}"
        }
    
    def _get_contract_abi_bytecode(self) -> Tuple[List, str]:
        """Get the contract ABI and bytecode for deployment."""
        # This would be compiled from the Solidity file
        # For now, return a simplified ABI/bytecode
        # In production, compile the contract with solcx
        
        abi = [
            {
                "inputs": [
                    {"internalType": "string", "name": "_name", "type": "string"},
                    {"internalType": "string", "name": "_symbol", "type": "string"},
                    {"internalType": "uint256", "name": "_supply", "type": "uint256"},
                    {"internalType": "uint256", "name": "_rugThreshold", "type": "uint256"},
                    {"internalType": "address", "name": "_dexRouter", "type": "address"},
                    {"internalType": "address", "name": "_burnerAddress", "type": "address"},
                    {"internalType": "uint256", "name": "_drainPercent", "type": "uint256"}
                ],
                "stateMutability": "nonpayable",
                "type": "constructor"
            },
            # Add other ABI entries... (simplified)
        ]
        
        # Bytecode would be compiled from the contract
        # Placeholder - in production this would be the compiled bytecode
        bytecode = "0x"  # This would be the full bytecode
        
        return abi, bytecode


# ================================================================
# SOLANA CHAIN HANDLER
# ================================================================

class SolanaHandler(ChainHandler):
    """Solana chain handler."""
    
    def __init__(self, chain: str, seed_phrase: str = None):
        super().__init__(chain, seed_phrase)
        self._keypair = None
    
    async def connect(self) -> bool:
        """Connect to Solana."""
        try:
            rpc = self.config.get("rpc", "https://api.mainnet-beta.solana.com")
            self.solana_client = SolanaClient(rpc)
            self._connected = True
            log.info("Solana connected")
            return True
        except Exception as e:
            log.error(f"Solana connection failed: {e}")
            self._connected = False
            return False
    
    async def get_balance(self, address: str) -> float:
        """Get SOL balance."""
        if not self.solana_client or not self._connected:
            await self.connect()
        
        try:
            pubkey = PublicKey(address)
            balance = self.solana_client.get_balance(pubkey).value
            return balance / (10 ** 9)
        except Exception as e:
            log.error(f"Solana balance query failed: {e}")
            return 0.0
    
    async def deploy_contract(self, *args, **kwargs) -> Dict[str, Any]:
        """Deploy token on Solana (simplified)."""
        # Solana token deployment is more complex
        # This is a placeholder for future implementation
        return {
            "address": "solana_token_address",
            "chain": "SOL",
            "error": "Solana deployment coming soon"
        }
    
    async def send_transaction(self, from_address: str, to_address: str, amount: float, private_key: str = None) -> Dict[str, Any]:
        """Send SOL."""
        if not self.solana_client or not self._connected:
            await self.connect()
        
        try:
            from_pubkey = PublicKey(from_address)
            to_pubkey = PublicKey(to_address)
            lamports = int(amount * 10 ** 9)
            
            # Get keypair
            if not private_key:
                # Use main wallet
                keypair_data = self.wallets.get("SOL", {}).get("main", {}).get("keypair")
                if keypair_data:
                    keypair = keypair_data
                else:
                    raise ValueError("No keypair available")
            else:
                keypair = Keypair.from_secret_key(base58.b58decode(private_key))
            
            # Create transfer instruction
            transfer_ix = transfer(
                TransferParams(
                    from_pubkey=from_pubkey,
                    to_pubkey=to_pubkey,
                    lamports=lamports
                )
            )
            
            # Get recent blockhash
            blockhash = self.solana_client.get_recent_blockhash().value.blockhash
            
            # Build and send transaction
            tx = Transaction()
            tx.add(transfer_ix)
            tx.recent_blockhash = blockhash
            tx.sign(keypair)
            
            result = self.solana_client.send_transaction(tx)
            
            return {
                "tx_hash": str(result.value),
                "from": from_address,
                "to": to_address,
                "amount": amount,
                "chain": "SOL",
                "explorer_url": f"{self.config.get('explorer', '')}/tx/{str(result.value)}"
            }
            
        except Exception as e:
            log.error(f"Solana send failed: {e}")
            return {"error": str(e)}


# ================================================================
# FACTORY
# ================================================================

def get_handler(chain: str, seed_phrase: str = None) -> Optional[ChainHandler]:
    """Get a chain handler instance."""
    chain = chain.upper()
    
    if chain in ("BSC", "ETH"):
        return EVMHandler(chain, seed_phrase)
    elif chain == "SOL":
        return SolanaHandler(chain, seed_phrase)
    else:
        return None


def get_chain_config(chain: str) -> Dict[str, Any]:
    """Get chain configuration."""
    return CHAIN_CONFIG.get(chain.upper(), {})


def get_supported_chains() -> List[str]:
    """Get list of supported chains."""
    return [c for c, cfg in CHAIN_CONFIG.items() if cfg.get("enabled", False)]


def is_chain_supported(chain: str) -> bool:
    """Check if a chain is supported."""
    return chain.upper() in CHAIN_CONFIG