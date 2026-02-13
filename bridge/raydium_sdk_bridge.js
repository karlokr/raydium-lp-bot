/**
 * Raydium SDK Bridge
 * 
 * Node.js bridge that uses the official Raydium SDK to execute
 * liquidity provision transactions on behalf of the Python bot.
 * 
 * Fetches complete pool keys by reading on-chain account data
 * (AMM state + Serum market state) and deriving authority PDAs.
 * 
 * Commands:
 *   node raydium_sdk_bridge.js add <poolId> <amountA> <amountB> <slippage>
 *   node raydium_sdk_bridge.js remove <poolId> <lpAmount> <slippage>
 *   node raydium_sdk_bridge.js balance <tokenMint>
 *   node raydium_sdk_bridge.js poolkeys <poolId>
 *   node raydium_sdk_bridge.js test
 */

import { Connection, Keypair, PublicKey, Transaction } from '@solana/web3.js';
import RaydiumSDK from '@raydium-io/raydium-sdk';
import { TOKEN_PROGRAM_ID, getAssociatedTokenAddress, createAssociatedTokenAccountInstruction } from '@solana/spl-token';
import BN from 'bn.js';
import Decimal from 'decimal.js';

const {
    Liquidity,
    Market,
    Token,
    TokenAmount,
    Percent,
    SPL_ACCOUNT_LAYOUT,
    LIQUIDITY_STATE_LAYOUT_V4,
    MARKET_STATE_LAYOUT_V3,
} = RaydiumSDK;

// Load environment variables
const RPC_URL = process.env.SOLANA_RPC_URL || 'https://api.mainnet-beta.solana.com';
const PRIVATE_KEY = process.env.WALLET_PRIVATE_KEY;

if (!PRIVATE_KEY && process.argv[2] !== 'test') {
    console.error('Error: WALLET_PRIVATE_KEY not found in environment');
    process.exit(1);
}

// Initialize connection and wallet
const connection = new Connection(RPC_URL, 'confirmed');
let wallet;

if (PRIVATE_KEY) {
    try {
        let secretKey;
        if (PRIVATE_KEY.includes(',')) {
            secretKey = Uint8Array.from(
                PRIVATE_KEY.replace(/[\[\]]/g, '').split(',').map(x => parseInt(x.trim()))
            );
        } else {
            const bs58 = await import('bs58');
            secretKey = bs58.default.decode(PRIVATE_KEY);
        }
        wallet = Keypair.fromSecretKey(secretKey);
    } catch (err) {
        console.error('Error loading wallet:', err.message);
        process.exit(1);
    }
}

/**
 * Fetch complete pool keys by reading on-chain accounts.
 * 
 * 1. Read AMM account -> decode with LIQUIDITY_STATE_LAYOUT_V4
 * 2. Read Market account -> decode with MARKET_STATE_LAYOUT_V3
 * 3. Derive AMM authority via Liquidity.getAssociatedAuthority()
 * 4. Derive Market authority via Market.getAssociatedAuthority()
 */
async function fetchPoolKeys(poolId) {
    const poolPubkey = new PublicKey(poolId);
    
    // Step 1: Read AMM account on-chain
    console.error('Fetching AMM account: ' + poolId);
    const ammAccountInfo = await connection.getAccountInfo(poolPubkey);
    if (!ammAccountInfo) {
        throw new Error('AMM account ' + poolId + ' not found on-chain');
    }
    
    const ammProgramId = ammAccountInfo.owner;
    console.error('AMM program owner: ' + ammProgramId.toString());
    
    // Decode AMM state using the SDK layout
    const ammState = LIQUIDITY_STATE_LAYOUT_V4.decode(ammAccountInfo.data);
    
    console.error('AMM state decoded successfully');
    console.error('  baseMint: ' + ammState.baseMint.toString());
    console.error('  quoteMint: ' + ammState.quoteMint.toString());
    console.error('  lpMint: ' + ammState.lpMint.toString());
    console.error('  marketId: ' + ammState.marketId.toString());
    console.error('  openOrders: ' + ammState.openOrders.toString());
    console.error('  targetOrders: ' + ammState.targetOrders.toString());
    
    // Step 2: Read Market (Serum/OpenBook) account on-chain
    const marketId = ammState.marketId;
    const marketProgramId = ammState.marketProgramId;
    
    console.error('Fetching Market account: ' + marketId.toString());
    const marketAccountInfo = await connection.getAccountInfo(marketId);
    if (!marketAccountInfo) {
        throw new Error('Market account ' + marketId.toString() + ' not found on-chain');
    }
    
    const marketState = MARKET_STATE_LAYOUT_V3.decode(marketAccountInfo.data);
    
    console.error('Market state decoded successfully');
    console.error('  marketBaseVault: ' + marketState.baseVault.toString());
    console.error('  marketQuoteVault: ' + marketState.quoteVault.toString());
    console.error('  marketBids: ' + marketState.bids.toString());
    console.error('  marketAsks: ' + marketState.asks.toString());
    console.error('  marketEventQueue: ' + marketState.eventQueue.toString());
    
    // Step 3: Derive AMM authority
    const { publicKey: authority } = Liquidity.getAssociatedAuthority({ programId: ammProgramId });
    console.error('  ammAuthority: ' + authority.toString());
    
    // Step 4: Derive Market authority
    const { publicKey: marketAuthority } = Market.getAssociatedAuthority({
        programId: marketProgramId,
        marketId: marketId,
    });
    console.error('  marketAuthority: ' + marketAuthority.toString());
    
    // Construct complete pool keys
    const poolKeys = {
        id: poolPubkey,
        baseMint: ammState.baseMint,
        quoteMint: ammState.quoteMint,
        lpMint: ammState.lpMint,
        baseDecimals: ammState.baseDecimal.toNumber(),
        quoteDecimals: ammState.quoteDecimal.toNumber(),
        lpDecimals: ammState.baseDecimal.toNumber(),
        version: 4,
        programId: ammProgramId,
        authority: authority,
        openOrders: ammState.openOrders,
        targetOrders: ammState.targetOrders,
        baseVault: ammState.baseVault,
        quoteVault: ammState.quoteVault,
        withdrawQueue: ammState.withdrawQueue,
        lpVault: ammState.lpVault,
        marketVersion: 3,
        marketProgramId: marketProgramId,
        marketId: marketId,
        marketAuthority: marketAuthority,
        marketBaseVault: marketState.baseVault,
        marketQuoteVault: marketState.quoteVault,
        marketBids: marketState.bids,
        marketAsks: marketState.asks,
        marketEventQueue: marketState.eventQueue,
        lookupTableAccount: PublicKey.default,
    };
    
    console.error('Pool keys constructed successfully');
    
    return poolKeys;
}

/**
 * Get all token accounts for the wallet owner
 */
async function getOwnerTokenAccounts() {
    const tokenResp = await connection.getTokenAccountsByOwner(wallet.publicKey, {
        programId: TOKEN_PROGRAM_ID,
    });
    
    const accounts = [];
    for (const { pubkey, account } of tokenResp.value) {
        accounts.push({
            pubkey,
            programId: TOKEN_PROGRAM_ID,
            accountInfo: SPL_ACCOUNT_LAYOUT.decode(account.data),
        });
    }
    
    return accounts;
}

/**
 * Get or create associated token account
 */
async function getOrCreateTokenAccount(mint, owner) {
    const ata = await getAssociatedTokenAddress(mint, owner);
    
    const accountInfo = await connection.getAccountInfo(ata);
    if (accountInfo) {
        return ata;
    }
    
    console.error('Creating ATA for mint: ' + mint.toString());
    const ix = createAssociatedTokenAccountInstruction(owner, ata, owner, mint);
    const tx = new Transaction().add(ix);
    const { blockhash } = await connection.getLatestBlockhash();
    tx.recentBlockhash = blockhash;
    tx.feePayer = owner;
    const signature = await connection.sendTransaction(tx, [wallet]);
    await connection.confirmTransaction(signature, 'confirmed');
    console.error('ATA created: ' + ata.toString());
    
    return ata;
}

/**
 * Add liquidity to a Raydium pool
 */
async function addLiquidity(poolId, amountA, amountB, slippage) {
    try {
        console.error('Adding liquidity to pool ' + poolId);
        console.error('Amount A: ' + amountA + ', Amount B: ' + amountB + ', Slippage: ' + slippage + '%');
        
        const poolKeys = await fetchPoolKeys(poolId);
        const tokenAccounts = await getOwnerTokenAccounts();
        
        // Token(programId, mint, decimals, symbol, name)
        const baseToken = new Token(TOKEN_PROGRAM_ID, poolKeys.baseMint, poolKeys.baseDecimals, 'BASE', 'Base Token');
        const quoteToken = new Token(TOKEN_PROGRAM_ID, poolKeys.quoteMint, poolKeys.quoteDecimals, 'QUOTE', 'Quote Token');
        
        const baseAmountRaw = new BN(new Decimal(amountA).mul(new Decimal(10).pow(poolKeys.baseDecimals)).floor().toString());
        const quoteAmountRaw = new BN(new Decimal(amountB).mul(new Decimal(10).pow(poolKeys.quoteDecimals)).floor().toString());
        
        const amountInA = new TokenAmount(baseToken, baseAmountRaw, true);
        const amountInB = new TokenAmount(quoteToken, quoteAmountRaw, true);
        
        console.error('Base amount raw: ' + baseAmountRaw.toString());
        console.error('Quote amount raw: ' + quoteAmountRaw.toString());
        
        // Ensure token accounts exist
        await getOrCreateTokenAccount(poolKeys.baseMint, wallet.publicKey);
        await getOrCreateTokenAccount(poolKeys.quoteMint, wallet.publicKey);
        await getOrCreateTokenAccount(poolKeys.lpMint, wallet.publicKey);
        
        // Re-fetch after creating
        const updatedTokenAccounts = await getOwnerTokenAccounts();
        
        const { innerTransactions } = await Liquidity.makeAddLiquidityInstructionSimple({
            connection,
            poolKeys,
            userKeys: {
                tokenAccounts: updatedTokenAccounts,
                owner: wallet.publicKey,
            },
            amountInA,
            amountInB,
            fixedSide: 'a',
            config: {
                bypassAssociatedCheck: false,
                checkCreateATAOwner: true,
            },
            makeTxVersion: 0,
        });
        
        const txids = [];
        for (const itemIxs of innerTransactions) {
            const tx = new Transaction();
            tx.add(...itemIxs.instructions);
            
            const { blockhash } = await connection.getLatestBlockhash();
            tx.recentBlockhash = blockhash;
            tx.feePayer = wallet.publicKey;
            
            const signers = [wallet];
            if (itemIxs.signers && itemIxs.signers.length > 0) {
                signers.push(...itemIxs.signers);
            }
            
            const signature = await connection.sendTransaction(tx, signers);
            await connection.confirmTransaction(signature, 'confirmed');
            txids.push(signature);
            console.error('Transaction confirmed: ' + signature);
        }
        
        console.log(JSON.stringify({
            success: true,
            signatures: txids,
            lpMint: poolKeys.lpMint.toString(),
        }));
        
    } catch (err) {
        console.log(JSON.stringify({
            success: false,
            error: err.message,
            stack: err.stack,
        }));
        process.exit(1);
    }
}

/**
 * Remove liquidity from a Raydium pool
 */
async function removeLiquidity(poolId, lpAmount, slippage) {
    try {
        console.error('Removing ' + lpAmount + ' LP tokens from pool ' + poolId);
        
        const poolKeys = await fetchPoolKeys(poolId);
        const tokenAccounts = await getOwnerTokenAccounts();
        
        const lpToken = new Token(TOKEN_PROGRAM_ID, poolKeys.lpMint, poolKeys.lpDecimals, 'LP', 'LP Token');
        
        const amountIn = new TokenAmount(
            lpToken,
            new BN(new Decimal(lpAmount).mul(new Decimal(10).pow(poolKeys.lpDecimals)).floor().toString()),
            true
        );
        
        const { innerTransactions } = await Liquidity.makeRemoveLiquidityInstructionSimple({
            connection,
            poolKeys,
            userKeys: {
                tokenAccounts: tokenAccounts,
                owner: wallet.publicKey,
            },
            amountIn,
            config: {
                bypassAssociatedCheck: false,
                checkCreateATAOwner: true,
            },
            makeTxVersion: 0,
        });
        
        const txids = [];
        for (const itemIxs of innerTransactions) {
            const tx = new Transaction();
            tx.add(...itemIxs.instructions);
            
            const { blockhash } = await connection.getLatestBlockhash();
            tx.recentBlockhash = blockhash;
            tx.feePayer = wallet.publicKey;
            
            const signers = [wallet];
            if (itemIxs.signers && itemIxs.signers.length > 0) {
                signers.push(...itemIxs.signers);
            }
            
            const signature = await connection.sendTransaction(tx, signers);
            await connection.confirmTransaction(signature, 'confirmed');
            txids.push(signature);
            console.error('Transaction confirmed: ' + signature);
        }
        
        console.log(JSON.stringify({
            success: true,
            signatures: txids,
        }));
        
    } catch (err) {
        console.log(JSON.stringify({
            success: false,
            error: err.message,
        }));
        process.exit(1);
    }
}

/**
 * Get token balance
 */
async function getBalance(tokenMint) {
    try {
        const mint = new PublicKey(tokenMint);
        const ata = await getAssociatedTokenAddress(mint, wallet.publicKey);
        
        const accountInfo = await connection.getAccountInfo(ata);
        if (!accountInfo) {
            console.log(JSON.stringify({ balance: 0 }));
            return;
        }
        
        const decoded = SPL_ACCOUNT_LAYOUT.decode(accountInfo.data);
        // decoded.amount is already a BN from the layout decoder
        const balance = decoded.amount.toString();
        
        console.log(JSON.stringify({ balance }));
        
    } catch (err) {
        console.log(JSON.stringify({
            success: false,
            error: err.message,
        }));
        process.exit(1);
    }
}

/**
 * Test fetching pool keys for a specific pool
 */
async function testPoolKeys(poolId) {
    try {
        console.error('Fetching pool keys for: ' + poolId);
        const poolKeys = await fetchPoolKeys(poolId);
        
        const formatted = {};
        for (const [key, value] of Object.entries(poolKeys)) {
            if (value instanceof PublicKey) {
                formatted[key] = value.toString();
            } else {
                formatted[key] = value;
            }
        }
        
        console.log(JSON.stringify({
            success: true,
            poolKeys: formatted,
        }, null, 2));
        
    } catch (err) {
        console.log(JSON.stringify({
            success: false,
            error: err.message,
            stack: err.stack,
        }));
        process.exit(1);
    }
}

/**
 * Test connection
 */
async function test() {
    try {
        if (!wallet) {
            console.log(JSON.stringify({
                success: false,
                error: 'No wallet loaded - set WALLET_PRIVATE_KEY in environment',
            }));
            process.exit(1);
        }
        const balance = await connection.getBalance(wallet.publicKey);
        console.log(JSON.stringify({
            success: true,
            pubkey: wallet.publicKey.toString(),
            balance: balance / 1e9,
            rpc: RPC_URL,
        }));
    } catch (err) {
        console.log(JSON.stringify({
            success: false,
            error: err.message,
        }));
        process.exit(1);
    }
}

// CLI interface
const command = process.argv[2];

switch (command) {
    case 'add':
        addLiquidity(process.argv[3], parseFloat(process.argv[4]), parseFloat(process.argv[5]), parseFloat(process.argv[6] || 1));
        break;
    case 'remove':
        removeLiquidity(process.argv[3], parseFloat(process.argv[4]), parseFloat(process.argv[5] || 1));
        break;
    case 'balance':
        getBalance(process.argv[3]);
        break;
    case 'poolkeys':
        testPoolKeys(process.argv[3]);
        break;
    case 'test':
        test();
        break;
    default:
        console.error('Unknown command. Usage:');
        console.error('  node raydium_sdk_bridge.js add <poolId> <amountA> <amountB> [slippage]');
        console.error('  node raydium_sdk_bridge.js remove <poolId> <lpAmount> [slippage]');
        console.error('  node raydium_sdk_bridge.js balance <tokenMint>');
        console.error('  node raydium_sdk_bridge.js poolkeys <poolId>');
        console.error('  node raydium_sdk_bridge.js test');
        process.exit(1);
}
