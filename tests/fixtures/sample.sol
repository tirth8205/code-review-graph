// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import "@openzeppelin/contracts/access/Ownable.sol";
import {IERC20, IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";

// ─── Protocol constants ─────────────────────────────────────────────────────

uint256 constant MAX_SUPPLY = 1_000_000_000 ether;
address constant ZERO_ADDRESS = address(0);

// ─── Types ──────────────────────────────────────────────────────────────────

/// @notice Staker position tracked per epoch.
struct StakerPosition {
    address wallet;
    uint256 stakedAmount;
    uint256 rewardDebt;
    uint64  epochJoined;
    bool    isActive;
}

/// @notice Pool lifecycle.
enum PoolStatus {
    Active,
    Paused,
    Deprecated,
    EmergencyShutdown
}

/// @notice 18-decimal fixed-point price.
type Price is uint256;

/// @notice Position receipt NFT identifier.
type PositionId is uint128;

// ─── Errors ─────────────────────────────────────────────────────────────────

error InsufficientStake(uint256 requested, uint256 available);
error PoolNotActive();

// ─── Events ─────────────────────────────────────────────────────────────────

event Staked(address indexed user, uint256 amount);
event Unstaked(address indexed user, uint256 amount);

// ─── Helpers ────────────────────────────────────────────────────────────────

/// @notice 30 bp protocol fee.
function protocolFee(uint256 amount) pure returns (uint256) {
    return (amount * 30) / 10_000;
}

// ─── Interface ──────────────────────────────────────────────────────────────

interface IStakingPool {
    function stake(uint256 amount) external;
    function unstake(uint256 amount) external returns (uint256);
    function stakedBalance(address user) external view returns (uint256);
}

// ─── Library ────────────────────────────────────────────────────────────────

/// @notice Fixed-point math for reward accumulator precision.
library RewardMath {
    uint256 internal constant PRECISION = 1e18;

    function mulPrecise(uint256 a, uint256 b) internal pure returns (uint256) {
        return (a * b) / PRECISION;
    }

    function divPrecise(uint256 a, uint256 b) internal pure returns (uint256) {
        require(b > 0, "RewardMath: division by zero");
        return (a * PRECISION) / b;
    }
}

// ─── Core pool ──────────────────────────────────────────────────────────────

/// @title StakingVault
/// @notice Liquid staking pool. Deposit the underlying ERC-20, receive
///         share tokens 1 : 1, accrue rewards over time.
contract StakingVault is ERC20, Ownable, IStakingPool {
    using RewardMath for uint256;

    // ── Storage ────────────────────────────────────────────────────────

    mapping(address => uint256) public stakes;
    uint256 public totalStaked;
    address public guardian;
    PoolStatus public status;
    uint256 constant MIN_STAKE = 0.01 ether;
    uint256 immutable launchTime;
    Price public assetPrice;
    uint256 public accRewardPerShare;

    // ── Events ─────────────────────────────────────────────────────────

    event RewardAccrued(uint256 indexed epoch, uint256 amount);
    event EmergencyExit(address indexed user, uint256 amount);

    // ── Modifiers ──────────────────────────────────────────────────────

    modifier nonZero(uint256 amount) {
        require(amount > 0, "StakingVault: zero amount");
        _;
    }

    modifier whenPoolActive() {
        require(status == PoolStatus.Active, "StakingVault: pool not active");
        _;
    }

    // ── Constructor ────────────────────────────────────────────────────

    constructor(
        string memory name,
        string memory symbol
    ) ERC20(name, symbol) Ownable(msg.sender) {
        guardian    = msg.sender;
        launchTime = block.timestamp;
        status     = PoolStatus.Active;
    }

    // ── Core operations ────────────────────────────────────────────────

    /// @inheritdoc IStakingPool
    function stake(uint256 amount)
        external
        override
        nonZero(amount)
        whenPoolActive
    {
        uint256 fee = protocolFee(amount);
        uint256 net = amount - fee;

        stakes[msg.sender] += net;
        totalStaked        += net;

        _mint(msg.sender, net);
        emit Staked(msg.sender, net);
    }

    /// @inheritdoc IStakingPool
    function unstake(uint256 amount)
        external
        override
        nonZero(amount)
        returns (uint256)
    {
        uint256 staked = stakes[msg.sender];
        if (staked < amount) {
            revert InsufficientStake(amount, staked);
        }

        stakes[msg.sender] = staked - amount;
        totalStaked        -= amount;

        _burn(msg.sender, amount);
        emit Unstaked(msg.sender, amount);
        return amount;
    }

    /// @inheritdoc IStakingPool
    function stakedBalance(address user) external view returns (uint256) {
        return stakes[user];
    }

    // ── Emergency ──────────────────────────────────────────────────────

    function emergencyWithdraw() external nonZero(stakes[msg.sender]) {
        uint256 amount = stakes[msg.sender];
        stakes[msg.sender] = 0;
        totalStaked        -= amount;

        _burn(msg.sender, amount);
        emit EmergencyExit(msg.sender, amount);
    }

    // ── ETH handling (native staking variant) ──────────────────────────

    receive() external payable {}
    fallback() external payable {}
}

// ─── Boosted pool ───────────────────────────────────────────────────────────

/// @title BoostedPool
/// @notice Wraps StakingVault with an additional reward layer.
///         Depositors earn base yield from the vault plus bonus
///         rewards funded by governance.
contract BoostedPool is StakingVault {
    uint256 public bonusRate;

    event BonusClaimed(address indexed user, uint256 reward);

    constructor(
        string memory name,
        string memory symbol,
        uint256 _bonusRate
    ) StakingVault(name, symbol) {
        bonusRate = _bonusRate;
    }

    function pendingBonus(address user) public view returns (uint256) {
        if (totalStaked == 0) return 0;
        return stakes[user].mulPrecise(bonusRate);
    }

    function claimBonus() external {
        uint256 reward = pendingBonus(msg.sender);
        require(reward > 0, "BoostedPool: nothing to claim");

        _mint(msg.sender, reward);
        emit BonusClaimed(msg.sender, reward);
    }
}
