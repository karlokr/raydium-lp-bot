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

import { Connection, Keypair, PublicKey, Transaction, SystemProgram } from '@solana/web3.js';
import RaydiumSDK from '@raydium-io/raydium-sdk';
import { TOKEN_PROGRAM_ID, getAssociatedTokenAddress, createAssociatedTokenAccountInstruction, createSyncNativeInstruction, createCloseAccountInstruction, NATIVE_MINT } from '@solana/spl-token';
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

const WSOL_MINT = new PublicKey('So11111111111111111111111111111111111111112');

// Load environment variables
const RPC_URL = process.env.SOLANA_RPC_URL || 'https://api.mainnet-beta.solana.com';
const PRIVATE_KEY = process.env.WALLET_PRIVATE_KEY;

if (!PRIVATE_KEY && process.argv[2] !== 'test') {
    console.error('Error: WALLET_PRIVATE_KEY not found in environment');
    process.exit(1);
}

// Initialize connection and wallet
// Patch Connection to avoid JSON-RPC batch requests (not supported on free Helius plans).
// @solana/web3.js auto-batches concurrent RPC calls into a single HTTP POST array.
// We override _rpcBatchRequest to execute them sequentially instead.
const connection = new Connection(RPC_URL, 'confirmed');

const origBatchRequest = connection._rpcBatchRequest.bind(connection);
connection._rpcBatchRequest = async function(requests) {
    console.error('[RPC] Intercepted batch of ' + requests.length + ' requests — running sequentially');
    const results = [];
    for (const req of requests) {
        try {
            const result = await connection._rpcRequest(req.methodName, req.args);
            results.push(result);
        } catch (e) {
            console.error('[RPC] Individual request failed: ' + req.methodName + ' — ' + e.message);
            results.push({ error: { code: -1, message: e.message } });
        }
    }
    return results;
};
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
        // AMM PnL fields — needed for correct reserve calculation
        // On-chain formula: effectiveReserve = vault + openOrders - needTakePnl
        baseNeedTakePnl: new BN(ammState.baseNeedTakePnl.toString()),
        quoteNeedTakePnl: new BN(ammState.quoteNeedTakePnl.toString()),
        lpReserve: new BN(ammState.lpReserve.toString()),
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
 * Add liquidity to a Raydium pool.
 * 
 * Reads the actual non-SOL token balance from the wallet and current pool
 * reserves to compute the correct amounts. This avoids stale pre-calculated
 * values (e.g. after a swap that moved the pool price).
 * 
 * amountSOL is the MAX SOL to contribute (the pool ratio determines the actual amount).
 */
async function addLiquidity(poolId, amountA, amountB, slippage) {
    try {
        console.error('Adding liquidity to pool ' + poolId);
        console.error('Requested A: ' + amountA + ', B: ' + amountB + ', Slippage: ' + slippage + '%');
        
        const poolKeys = await fetchPoolKeys(poolId);
        
        const baseIsWsol = poolKeys.baseMint.equals(WSOL_MINT);
        const quoteIsWsol = poolKeys.quoteMint.equals(WSOL_MINT);
        
        // Ensure token accounts exist
        const baseAta = await getOrCreateTokenAccount(poolKeys.baseMint, wallet.publicKey);
        const quoteAta = await getOrCreateTokenAccount(poolKeys.quoteMint, wallet.publicKey);
        await getOrCreateTokenAccount(poolKeys.lpMint, wallet.publicKey);
        
        // Read actual non-SOL token balance from wallet
        const nonSolMint = baseIsWsol ? poolKeys.quoteMint : poolKeys.baseMint;
        const nonSolAta = baseIsWsol ? quoteAta : baseAta;
        const nonSolDecimals = baseIsWsol ? poolKeys.quoteDecimals : poolKeys.baseDecimals;
        const solDecimals = baseIsWsol ? poolKeys.baseDecimals : poolKeys.quoteDecimals;
        
        const nonSolAcctInfo = await connection.getAccountInfo(nonSolAta);
        if (!nonSolAcctInfo) {
            throw new Error('Non-SOL token account not found. Did the swap succeed?');
        }
        const nonSolDecoded = SPL_ACCOUNT_LAYOUT.decode(nonSolAcctInfo.data);
        const actualNonSolRaw = new BN(nonSolDecoded.amount.toString());
        const actualNonSol = parseFloat(new Decimal(actualNonSolRaw.toString()).div(new Decimal(10).pow(nonSolDecimals)).toString());
        
        console.error('Actual non-SOL token balance: ' + actualNonSol + ' (raw: ' + actualNonSolRaw.toString() + ')');
        
        // Read pool vaults AND OpenOrders account to compute effective reserves.
        // The AMM on-chain uses: effectiveReserve = vaultAmount + openOrdersTotal
        // Using only vault amounts causes 0x1e slippage because the ratio is wrong.
        const [baseVaultInfo, quoteVaultInfo, openOrdersInfo, lpMintInfo] = await Promise.all([
            connection.getAccountInfo(poolKeys.baseVault),
            connection.getAccountInfo(poolKeys.quoteVault),
            connection.getAccountInfo(poolKeys.openOrders),
            connection.getAccountInfo(poolKeys.lpMint),
        ]);
        if (!baseVaultInfo || !quoteVaultInfo) {
            throw new Error('Could not read pool vault accounts');
        }
        const baseVaultAmount = new BN(SPL_ACCOUNT_LAYOUT.decode(baseVaultInfo.data).amount.toString());
        const quoteVaultAmount = new BN(SPL_ACCOUNT_LAYOUT.decode(quoteVaultInfo.data).amount.toString());
        console.error('Vault balances - base: ' + baseVaultAmount.toString() + ', quote: ' + quoteVaultAmount.toString());
        
        // Read OpenOrders totals (Serum V3 layout: baseTokenTotal at offset 85, quoteTokenTotal at offset 101, both u64 LE)
        let openOrdersBaseTotal = new BN(0);
        let openOrdersQuoteTotal = new BN(0);
        if (openOrdersInfo && openOrdersInfo.data.length >= 109) {
            openOrdersBaseTotal = new BN(openOrdersInfo.data.readBigUInt64LE(85).toString());
            openOrdersQuoteTotal = new BN(openOrdersInfo.data.readBigUInt64LE(101).toString());
            console.error('OpenOrders - baseTotal: ' + openOrdersBaseTotal.toString() + ', quoteTotal: ' + openOrdersQuoteTotal.toString());
        } else {
            console.error('OpenOrders account not readable, using vault-only reserves (may be inaccurate)');
        }
        
        // Effective reserves = vault + openOrders - needTakePnl
        // (matching on-chain calc_total_without_take_pnl)
        const baseReserve = baseVaultAmount.add(openOrdersBaseTotal).sub(poolKeys.baseNeedTakePnl);
        const quoteReserve = quoteVaultAmount.add(openOrdersQuoteTotal).sub(poolKeys.quoteNeedTakePnl);
        console.error('Effective reserves - base: ' + baseReserve.toString() + ', quote: ' + quoteReserve.toString());
        
        // Read LP supply from mint account
        let lpSupply = new BN(0);
        if (lpMintInfo) {
            lpSupply = new BN(lpMintInfo.data.readBigUInt64LE(36).toString());
        }
        
        // Build poolInfo for SDK's computeAnotherAmount
        const poolInfo = {
            status: new BN(1),
            baseDecimals: poolKeys.baseDecimals,
            quoteDecimals: poolKeys.quoteDecimals,
            lpDecimals: poolKeys.lpDecimals,
            baseReserve,
            quoteReserve,
            lpSupply,
            startTime: new BN(0),
        };
        
        // Use SDK's computeAnotherAmount to get the correct matching amount + slippage
        // Fix on the non-SOL token (the exact amount we got from the swap)
        const baseToken = new Token(TOKEN_PROGRAM_ID, poolKeys.baseMint, poolKeys.baseDecimals, 'BASE', 'Base Token');
        const quoteToken = new Token(TOKEN_PROGRAM_ID, poolKeys.quoteMint, poolKeys.quoteDecimals, 'QUOTE', 'Quote Token');
        
        const slippagePercent = new Percent(Math.round(slippage * 100), 10000);
        
        // The fixed input is the non-SOL token we hold
        let fixedTokenAmount;
        let fixedSide;  // 'a' or 'b' for makeAddLiquidityInstructionSimple
        if (baseIsWsol) {
            // base=WSOL, quote=token: we fix on quote (side 'b'), ask SDK to compute base
            fixedTokenAmount = new TokenAmount(quoteToken, actualNonSolRaw, true);
            fixedSide = 'b';
        } else {
            // base=token, quote=WSOL: we fix on base (side 'a'), ask SDK to compute quote
            fixedTokenAmount = new TokenAmount(baseToken, actualNonSolRaw, true);
            fixedSide = 'a';
        }
        
        const { anotherAmount, maxAnotherAmount } = Liquidity.computeAnotherAmount({
            poolKeys,
            poolInfo,
            amount: fixedTokenAmount,
            anotherCurrency: baseIsWsol ? baseToken : quoteToken,
            slippage: slippagePercent,
        });
        
        console.error('Fixed amount (' + (baseIsWsol ? 'quote' : 'base') + '): ' + actualNonSolRaw.toString());
        console.error('Computed other amount: ' + anotherAmount.toFixed() + ' (max with slippage: ' + maxAnotherAmount.toFixed() + ')');
        
        // Build final amountInA (base) and amountInB (quote) for the SDK call
        let amountInA, amountInB;
        if (baseIsWsol) {
            // base=WSOL: use maxAnotherAmount (with slippage) as the SOL side max
            amountInA = maxAnotherAmount;  // WSOL max (includes slippage tolerance)
            amountInB = fixedTokenAmount;  // exact token amount
        } else {
            // quote=WSOL: use maxAnotherAmount (with slippage) as the SOL side max
            amountInA = fixedTokenAmount;  // exact token amount
            amountInB = maxAnotherAmount;  // WSOL max (includes slippage tolerance)
        }
        
        console.error('Final amounts - amountInA: ' + amountInA.raw.toString() + ', amountInB: ' + amountInB.raw.toString());
        console.error('fixedSide: ' + fixedSide);
        console.error('Slippage: ' + slippage + '%');
        
        // Ensure enough native SOL for the SDK's temp WSOL account
        const wsolAmountRaw = baseIsWsol ? amountInA.raw : amountInB.raw;
        const wsolAta = baseIsWsol ? baseAta : quoteAta;
        const wrapLamports = BigInt(wsolAmountRaw.toString());
        const overhead = BigInt(7_000_000); // rent + fees
        const totalNeeded = wrapLamports + overhead;
        const nativeBal = BigInt(await connection.getBalance(wallet.publicKey));
        
        console.error('SDK needs ' + totalNeeded.toString() + ' native lamports, have ' + nativeBal.toString());
        
        if (nativeBal < totalNeeded) {
            const wsolAcctInfo = await connection.getAccountInfo(wsolAta);
            if (wsolAcctInfo) {
                const decoded = SPL_ACCOUNT_LAYOUT.decode(wsolAcctInfo.data);
                const wsolBalance = BigInt(decoded.amount.toString());
                console.error('Unwrapping WSOL ATA (' + wsolBalance.toString() + ' lamports) to get native SOL...');
                const unwrapTx = new Transaction();
                unwrapTx.add(createCloseAccountInstruction(wsolAta, wallet.publicKey, wallet.publicKey));
                const { blockhash: ub } = await connection.getLatestBlockhash();
                unwrapTx.recentBlockhash = ub;
                unwrapTx.feePayer = wallet.publicKey;
                const us = await connection.sendTransaction(unwrapTx, [wallet]);
                await connection.confirmTransaction(us, 'confirmed');
                console.error('WSOL unwrapped: ' + us);
            }
        }
        
        // Re-fetch token accounts
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
            fixedSide: fixedSide,
            slippage: slippagePercent,
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
        console.error('Removing LP from pool ' + poolId);
        
        const poolKeys = await fetchPoolKeys(poolId);
        const tokenAccounts = await getOwnerTokenAccounts();
        
        const lpToken = new Token(TOKEN_PROGRAM_ID, poolKeys.lpMint, poolKeys.lpDecimals, 'LP', 'LP Token');
        
        // Always use the exact on-chain LP balance to avoid rounding errors
        const lpAta = await getAssociatedTokenAddress(poolKeys.lpMint, wallet.publicKey);
        const lpAccountInfo = await connection.getAccountInfo(lpAta);
        if (!lpAccountInfo) {
            throw new Error('No LP token account found for mint ' + poolKeys.lpMint.toBase58());
        }
        const decoded = SPL_ACCOUNT_LAYOUT.decode(lpAccountInfo.data);
        const rawAmount = new BN(decoded.amount.toString());
        if (rawAmount.isZero()) {
            throw new Error('LP token balance is 0');
        }
        const uiAmount = new Decimal(rawAmount.toString()).div(new Decimal(10).pow(poolKeys.lpDecimals)).toString();
        console.error('  On-chain LP balance: ' + uiAmount + ' (raw: ' + rawAmount.toString() + ')');
        
        const amountIn = new TokenAmount(lpToken, rawAmount, true);
        
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

/**
 * Swap tokens via Raydium AMM pool
 * direction: 'buy' = swap WSOL -> non-WSOL token, 'sell' = swap non-WSOL token -> WSOL
 * Automatically detects which side of the pool is WSOL.
 */
async function swapTokens(poolId, amountIn, slippage, direction) {
    try {
        console.error('=== SWAP START ===');
        console.error('Direction: ' + direction + ' | Pool: ' + poolId);
        console.error('Amount in: ' + amountIn + ' | Slippage: ' + slippage + '%');
        
        // Step 1: Fetch pool keys
        console.error('[1/7] Fetching pool keys...');
        const poolKeys = await fetchPoolKeys(poolId);
        
        const baseIsWsol = poolKeys.baseMint.equals(WSOL_MINT);
        const quoteIsWsol = poolKeys.quoteMint.equals(WSOL_MINT);
        
        if (!baseIsWsol && !quoteIsWsol) {
            throw new Error('Neither base nor quote is WSOL - cannot swap');
        }
        
        console.error('  Base:  ' + poolKeys.baseMint.toString() + ' (decimals: ' + poolKeys.baseDecimals + ')' + (baseIsWsol ? ' [WSOL]' : ''));
        console.error('  Quote: ' + poolKeys.quoteMint.toString() + ' (decimals: ' + poolKeys.quoteDecimals + ')' + (quoteIsWsol ? ' [WSOL]' : ''));
        
        const baseToken = new Token(TOKEN_PROGRAM_ID, poolKeys.baseMint, poolKeys.baseDecimals, 'BASE', 'Base Token');
        const quoteToken = new Token(TOKEN_PROGRAM_ID, poolKeys.quoteMint, poolKeys.quoteDecimals, 'QUOTE', 'Quote Token');
        
        // Step 2: Determine swap direction
        console.error('[2/7] Determining swap direction...');
        let inputToken, outputToken;
        if (direction === 'buy') {
            if (baseIsWsol) {
                inputToken = baseToken;
                outputToken = quoteToken;
            } else {
                inputToken = quoteToken;
                outputToken = baseToken;
            }
        } else {
            if (baseIsWsol) {
                inputToken = quoteToken;
                outputToken = baseToken;
            } else {
                inputToken = baseToken;
                outputToken = quoteToken;
            }
        }
        
        const inputIsWsol = inputToken.mint.equals(WSOL_MINT);
        console.error('  Input:  ' + inputToken.mint.toString() + (inputIsWsol ? ' [WSOL]' : '') + ' (decimals: ' + inputToken.decimals + ')');
        console.error('  Output: ' + outputToken.mint.toString() + ' (decimals: ' + outputToken.decimals + ')');
        
        // Step 3: Ensure token accounts exist
        console.error('[3/7] Ensuring token accounts exist...');
        const baseAta = await getOrCreateTokenAccount(poolKeys.baseMint, wallet.publicKey);
        const quoteAta = await getOrCreateTokenAccount(poolKeys.quoteMint, wallet.publicKey);
        console.error('  Base ATA:  ' + baseAta.toString());
        console.error('  Quote ATA: ' + quoteAta.toString());
        
        // Step 4: Determine actual amount (sell-all mode if amount <= 0)
        console.error('[4/7] Determining swap amount...');
        let actualAmountIn = amountIn;
        if (amountIn <= 0 && !inputIsWsol) {
            const inputAta = inputToken.mint.equals(poolKeys.baseMint) ? baseAta : quoteAta;
            const acctInfo = await connection.getAccountInfo(inputAta);
            if (acctInfo) {
                const decoded = SPL_ACCOUNT_LAYOUT.decode(acctInfo.data);
                const rawBalance = new BN(decoded.amount.toString());
                if (rawBalance.isZero()) {
                    console.error('  Sell-all mode: token balance is 0, nothing to sell');
                    console.log(JSON.stringify({ success: true, signatures: [], note: 'No tokens to sell' }));
                    return;
                }
                actualAmountIn = parseFloat(new Decimal(rawBalance.toString()).div(new Decimal(10).pow(inputToken.decimals)).toString());
                console.error('  Sell-all mode: found ' + actualAmountIn + ' tokens (raw: ' + rawBalance.toString() + ')');
            } else {
                console.error('  Sell-all mode: no token account found');
                console.log(JSON.stringify({ success: true, signatures: [], note: 'No token account found' }));
                return;
            }
        }
        
        const amountInRaw = new BN(new Decimal(actualAmountIn).mul(new Decimal(10).pow(inputToken.decimals)).floor().toString());
        const amountInToken = new TokenAmount(inputToken, amountInRaw, true);
        const slippagePercent = new Percent(Math.round(slippage * 100), 10000);
        console.error('  Amount raw: ' + amountInRaw.toString());
        
        // Step 5: If input is WSOL, ensure we have enough native SOL
        // The SDK creates a TEMPORARY wrapped SOL account from native SOL for swaps.
        // It does NOT use the WSOL ATA. So we must unwrap any WSOL ATA balance
        // back to native SOL first, then let the SDK handle wrapping internally.
        if (inputIsWsol) {
            console.error('[5/7] Ensuring sufficient native SOL for SDK swap...');
            const wsolAta = baseIsWsol ? baseAta : quoteAta;
            
            const wrapLamports = BigInt(amountInRaw.toString());
            const feesReserve = BigInt(5_000_000); // 0.005 SOL for tx fees + rent
            const rentReserve = BigInt(2_039_280); // token account rent
            const totalNeeded = wrapLamports + rentReserve + feesReserve;
            
            const nativeBal = BigInt(await connection.getBalance(wallet.publicKey));
            console.error('  Native SOL: ' + nativeBal.toString() + ' lamports');
            console.error('  SDK needs: ' + totalNeeded.toString() + ' lamports (amount + rent + fees)');
            
            if (nativeBal < totalNeeded) {
                // Not enough native SOL — unwrap WSOL ATA to get more native SOL
                const wsolAcctInfo = await connection.getAccountInfo(wsolAta);
                if (wsolAcctInfo) {
                    const decoded = SPL_ACCOUNT_LAYOUT.decode(wsolAcctInfo.data);
                    const wsolBalance = BigInt(decoded.amount.toString());
                    console.error('  WSOL in ATA: ' + wsolBalance.toString() + ' lamports');
                    
                    if (nativeBal + wsolBalance + rentReserve >= totalNeeded) {
                        // Unwrap (close) the WSOL ATA — returns WSOL + rent to native SOL
                        console.error('  Unwrapping WSOL ATA to get native SOL...');
                        const unwrapTx = new Transaction();
                        unwrapTx.add(
                            createCloseAccountInstruction(wsolAta, wallet.publicKey, wallet.publicKey)
                        );
                        const { blockhash: ub } = await connection.getLatestBlockhash();
                        unwrapTx.recentBlockhash = ub;
                        unwrapTx.feePayer = wallet.publicKey;
                        const unwrapSig = await connection.sendTransaction(unwrapTx, [wallet]);
                        await connection.confirmTransaction(unwrapSig, 'confirmed');
                        console.error('  WSOL unwrapped: ' + unwrapSig);
                        
                        const newNativeBal = await connection.getBalance(wallet.publicKey);
                        console.error('  Native SOL after unwrap: ' + newNativeBal + ' lamports');
                    } else {
                        const haveTotal = Number(nativeBal + wsolBalance) / 1e9;
                        const needTotal = Number(totalNeeded) / 1e9;
                        throw new Error('Insufficient funds. Have ' + haveTotal.toFixed(4) + ' SOL total but need ' + needTotal.toFixed(4) + ' SOL (amount + rent + fees).');
                    }
                } else {
                    const haveSOL = (Number(nativeBal) / 1e9).toFixed(4);
                    const needSOL = (Number(totalNeeded) / 1e9).toFixed(4);
                    throw new Error('Insufficient native SOL. Have ' + haveSOL + ' SOL but need ' + needSOL + ' SOL and no WSOL ATA to unwrap.');
                }
            } else {
                console.error('  Native SOL is sufficient, no unwrap needed');
            }
        } else {
            console.error('[5/7] Input is not WSOL, skipping wrap');
            
            // Verify the input token account has enough balance
            const inputAta = inputToken.mint.equals(poolKeys.baseMint) ? baseAta : quoteAta;
            const inputAcctInfo = await connection.getAccountInfo(inputAta);
            if (!inputAcctInfo) {
                throw new Error('Input token account does not exist: ' + inputAta.toString());
            }
            const decoded = SPL_ACCOUNT_LAYOUT.decode(inputAcctInfo.data);
            const inputBalance = new BN(decoded.amount.toString());
            console.error('  Input token balance: ' + inputBalance.toString() + ' raw');
            if (inputBalance.lt(amountInRaw)) {
                throw new Error('Insufficient input token balance. Have ' + inputBalance.toString() + ' but need ' + amountInRaw.toString());
            }
        }
        
        // Step 6: Execute swap via SDK
        console.error('[6/7] Building swap instruction...');
        
        // Read vault balances + OpenOrders to compute effective reserves
        // (matches what the AMM program uses internally)
        const [baseVaultInfo, quoteVaultInfo, openOrdersInfo, lpMintInfo] = await Promise.all([
            connection.getAccountInfo(poolKeys.baseVault),
            connection.getAccountInfo(poolKeys.quoteVault),
            connection.getAccountInfo(poolKeys.openOrders),
            connection.getAccountInfo(poolKeys.lpMint),
        ]);
        if (!baseVaultInfo || !quoteVaultInfo) {
            throw new Error('Could not read vault accounts for pool reserves');
        }
        const baseVaultDecoded = SPL_ACCOUNT_LAYOUT.decode(baseVaultInfo.data);
        const quoteVaultDecoded = SPL_ACCOUNT_LAYOUT.decode(quoteVaultInfo.data);
        const baseVaultAmount = new BN(baseVaultDecoded.amount.toString());
        const quoteVaultAmount = new BN(quoteVaultDecoded.amount.toString());
        
        // Add OpenOrders totals for effective reserves
        let openOrdersBaseTotal = new BN(0);
        let openOrdersQuoteTotal = new BN(0);
        if (openOrdersInfo && openOrdersInfo.data.length >= 109) {
            openOrdersBaseTotal = new BN(openOrdersInfo.data.readBigUInt64LE(85).toString());
            openOrdersQuoteTotal = new BN(openOrdersInfo.data.readBigUInt64LE(101).toString());
            console.error('  OpenOrders - baseTotal: ' + openOrdersBaseTotal.toString() + ', quoteTotal: ' + openOrdersQuoteTotal.toString());
        }
        
        const baseReserve = baseVaultAmount.add(openOrdersBaseTotal);
        const quoteReserve = quoteVaultAmount.add(openOrdersQuoteTotal);
        
        // Parse LP supply from mint account (first 36 bytes: 4 mintAuthorityOption + 32 mintAuthority, then 8 bytes supply)
        let lpSupply = new BN(0);
        if (lpMintInfo) {
            // SPL Mint layout: supply is at offset 36, u64 LE
            lpSupply = new BN(lpMintInfo.data.readBigUInt64LE(36).toString());
        }
        
        const poolInfo = {
            status: new BN(1), // assume active
            baseDecimals: poolKeys.baseDecimals,
            quoteDecimals: poolKeys.quoteDecimals,
            lpDecimals: poolKeys.lpDecimals,
            baseReserve,
            quoteReserve,
            lpSupply,
            startTime: new BN(0),
        };
        
        console.error('  Pool reserves - base: ' + baseReserve.toString() + ', quote: ' + quoteReserve.toString());
        console.error('  LP supply: ' + lpSupply.toString());
        
        if (baseReserve.isZero() || quoteReserve.isZero()) {
            throw new Error('Pool has zero reserves (base: ' + baseReserve.toString() + ', quote: ' + quoteReserve.toString() + '). Pool may be empty or inactive.');
        }
        
        // Re-fetch token accounts after wrapping
        const updatedTokenAccounts = await getOwnerTokenAccounts();
        console.error('  Token accounts: ' + updatedTokenAccounts.length + ' found');
        
        // Log which accounts match input/output
        for (const acct of updatedTokenAccounts) {
            const mintStr = acct.accountInfo.mint.toString();
            if (mintStr === inputToken.mint.toString() || mintStr === outputToken.mint.toString()) {
                console.error('  Account ' + acct.pubkey.toString() + ' mint=' + mintStr + ' amount=' + acct.accountInfo.amount.toString());
            }
        }
        
        // Compute amounts first to catch errors early
        const { amountOut, minAmountOut } = Liquidity.computeAmountOut({
            poolKeys,
            poolInfo,
            amountIn: amountInToken,
            currencyOut: outputToken,
            slippage: slippagePercent,
        });
        
        console.error('  Expected out: ' + amountOut.toFixed() + ', Min out: ' + minAmountOut.toFixed());
        
        const { innerTransactions } = await Liquidity.makeSwapInstructionSimple({
            connection,
            poolKeys,
            userKeys: {
                tokenAccounts: updatedTokenAccounts,
                owner: wallet.publicKey,
            },
            amountIn: amountInToken,
            amountOut: minAmountOut,
            fixedSide: 'in',
            config: {
                bypassAssociatedCheck: false,
                checkCreateATAOwner: true,
            },
            makeTxVersion: 0,
        });
        
        // Step 7: Send transactions
        console.error('[7/7] Sending swap transaction(s)...');
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
            console.error('  Swap confirmed: ' + signature);
        }
        
        // Unwrap any remaining WSOL ATA balance back to native SOL after sell
        const outputIsWsol = outputToken.mint.equals(WSOL_MINT);
        if (outputIsWsol) {
            const wsolAta = baseIsWsol ? baseAta : quoteAta;
            const wsolAcctInfo = await connection.getAccountInfo(wsolAta);
            if (wsolAcctInfo) {
                const decoded = SPL_ACCOUNT_LAYOUT.decode(wsolAcctInfo.data);
                const remaining = BigInt(decoded.amount.toString());
                console.error('  WSOL ATA has ' + remaining.toString() + ' lamports remaining, unwrapping...');
                const unwrapTx = new Transaction();
                unwrapTx.add(
                    createCloseAccountInstruction(wsolAta, wallet.publicKey, wallet.publicKey)
                );
                const { blockhash: unwrapBlockhash } = await connection.getLatestBlockhash();
                unwrapTx.recentBlockhash = unwrapBlockhash;
                unwrapTx.feePayer = wallet.publicKey;
                const unwrapSig = await connection.sendTransaction(unwrapTx, [wallet]);
                await connection.confirmTransaction(unwrapSig, 'confirmed');
                console.error('  WSOL unwrapped: ' + unwrapSig);
            } else {
                console.error('  No WSOL ATA to unwrap (SDK handled it via temp account)');
            }
        }
        
        console.error('=== SWAP SUCCESS ===');
        console.log(JSON.stringify({
            success: true,
            signatures: txids,
            amountOut: amountOut.toFixed(),
            minAmountOut: minAmountOut.toFixed(),
        }));
        
    } catch (err) {
        console.error('=== SWAP FAILED ===');
        console.error('Error: ' + err.message);
        if (err.stack) console.error('Stack: ' + err.stack);
        if (err.logs) console.error('Logs: ' + JSON.stringify(err.logs));
        console.log(JSON.stringify({
            success: false,
            error: err.message,
            ...(err.logs ? { logs: err.logs } : {}),
        }));
        process.exit(1);
    }
}

/**
 * Unwrap all WSOL in the wallet's ATA back to native SOL
 */
async function unwrapWsol() {
    try {
        const wsolAta = await getAssociatedTokenAddress(WSOL_MINT, wallet.publicKey);
        const acctInfo = await connection.getAccountInfo(wsolAta);
        if (!acctInfo) {
            console.log(JSON.stringify({ success: true, unwrapped: 0, note: 'No WSOL ATA found' }));
            return;
        }
        const decoded = SPL_ACCOUNT_LAYOUT.decode(acctInfo.data);
        const balance = BigInt(decoded.amount.toString());
        // Closing the ATA returns the token balance + rent to the owner as native SOL
        const tx = new Transaction();
        tx.add(createCloseAccountInstruction(wsolAta, wallet.publicKey, wallet.publicKey));
        const { blockhash } = await connection.getLatestBlockhash();
        tx.recentBlockhash = blockhash;
        tx.feePayer = wallet.publicKey;
        const sig = await connection.sendTransaction(tx, [wallet]);
        await connection.confirmTransaction(sig, 'confirmed');
        const unwrapped = Number(balance) / 1e9;
        console.error('Unwrapped ' + unwrapped.toFixed(4) + ' WSOL → native SOL: ' + sig);
        console.log(JSON.stringify({ success: true, unwrapped, signature: sig }));
    } catch (err) {
        console.log(JSON.stringify({ success: false, error: err.message }));
        process.exit(1);
    }
}

/**
 * Compute the SOL value of wallet's LP tokens for a given pool.
 * Returns the proportional share of the pool in SOL terms.
 */
async function getLpValue(poolId, lpMint) {
    try {
        const poolKeys = await fetchPoolKeys(poolId);
        
        const baseIsWsol = poolKeys.baseMint.equals(WSOL_MINT);
        const quoteIsWsol = poolKeys.quoteMint.equals(WSOL_MINT);
        
        if (!baseIsWsol && !quoteIsWsol) {
            console.log(JSON.stringify({ valueSol: 0, error: 'No WSOL side in pool' }));
            return;
        }
        
        // Read LP token balance
        const lpMintPubkey = new PublicKey(lpMint);
        const lpAta = await getAssociatedTokenAddress(lpMintPubkey, wallet.publicKey);
        const lpAcctInfo = await connection.getAccountInfo(lpAta);
        if (!lpAcctInfo) {
            console.log(JSON.stringify({ valueSol: 0, lpBalance: 0 }));
            return;
        }
        const lpDecoded = SPL_ACCOUNT_LAYOUT.decode(lpAcctInfo.data);
        const lpBalance = new BN(lpDecoded.amount.toString());
        
        if (lpBalance.isZero()) {
            console.log(JSON.stringify({ valueSol: 0, lpBalance: 0 }));
            return;
        }
        
        // Read pool reserves + OpenOrders + LP supply
        const [baseVaultInfo, quoteVaultInfo, openOrdersInfo, lpMintInfo] = await Promise.all([
            connection.getAccountInfo(poolKeys.baseVault),
            connection.getAccountInfo(poolKeys.quoteVault),
            connection.getAccountInfo(poolKeys.openOrders),
            connection.getAccountInfo(poolKeys.lpMint),
        ]);
        
        const baseVault = new BN(SPL_ACCOUNT_LAYOUT.decode(baseVaultInfo.data).amount.toString());
        const quoteVault = new BN(SPL_ACCOUNT_LAYOUT.decode(quoteVaultInfo.data).amount.toString());
        
        let ooBase = new BN(0);
        let ooQuote = new BN(0);
        if (openOrdersInfo && openOrdersInfo.data.length >= 109) {
            ooBase = new BN(openOrdersInfo.data.readBigUInt64LE(85).toString());
            ooQuote = new BN(openOrdersInfo.data.readBigUInt64LE(101).toString());
        }
        
        const baseReserve = baseVault.add(ooBase).sub(poolKeys.baseNeedTakePnl);
        const quoteReserve = quoteVault.add(ooQuote).sub(poolKeys.quoteNeedTakePnl);
        const lpSupplyRaw = new BN(lpMintInfo.data.readBigUInt64LE(36).toString());
        // Circulating LP supply = total minted - protocol's LP reserve
        const lpSupply = lpSupplyRaw.sub(poolKeys.lpReserve);
        
        if (lpSupply.isZero()) {
            console.log(JSON.stringify({ valueSol: 0, error: 'LP supply is zero' }));
            return;
        }
        
        // Compute our share of each reserve
        // shareBase = lpBalance * baseReserve / lpSupply
        // shareQuote = lpBalance * quoteReserve / lpSupply
        const shareBase = lpBalance.mul(baseReserve).div(lpSupply);
        const shareQuote = lpBalance.mul(quoteReserve).div(lpSupply);
        
        // Convert to SOL value
        // If base is WSOL: solValue = shareBase + shareQuote * baseReserve / quoteReserve
        // If quote is WSOL: solValue = shareQuote + shareBase * quoteReserve / baseReserve
        let valueLamports;
        if (baseIsWsol) {
            // shareBase is already in SOL lamports
            // shareQuote in token units: convert via price = baseReserve/quoteReserve
            const quoteInSol = shareQuote.mul(baseReserve).div(quoteReserve);
            valueLamports = shareBase.add(quoteInSol);
        } else {
            const baseInSol = shareBase.mul(quoteReserve).div(baseReserve);
            valueLamports = shareQuote.add(baseInSol);
        }
        
        const valueSol = parseFloat(new Decimal(valueLamports.toString()).div(1e9).toString());
        
        // Compute on-chain price ratio (quote/base) using effective reserves
        // This matches the price convention used at entry: mintB_amount / mintA_amount
        const baseDecimals = poolKeys.baseDecimals;
        const quoteDecimals = poolKeys.quoteDecimals;
        // priceRatio = (quoteReserve / 10^quoteDecimals) / (baseReserve / 10^baseDecimals)
        const priceRatio = baseReserve.isZero() ? 0 : parseFloat(
            new Decimal(quoteReserve.toString())
                .div(new Decimal(10).pow(quoteDecimals))
                .div(new Decimal(baseReserve.toString()).div(new Decimal(10).pow(baseDecimals)))
                .toString()
        );
        
        console.log(JSON.stringify({
            valueSol,
            priceRatio,
            lpBalance: lpBalance.toString(),
            lpSupply: lpSupply.toString(),
            shareBase: shareBase.toString(),
            shareQuote: shareQuote.toString(),
        }));
        
    } catch (err) {
        console.log(JSON.stringify({ valueSol: 0, error: err.message }));
    }
}

/**
 * List all non-zero token accounts in the wallet (excluding native SOL).
 * Returns an array of {mint, balance} for each token with balance > 0.
 */
async function listTokens() {
    try {
        const tokenAccounts = await getOwnerTokenAccounts();
        const tokens = [];

        for (const acct of tokenAccounts) {
            const mint = acct.accountInfo.mint.toString();
            const rawBalance = acct.accountInfo.amount.toString();

            if (rawBalance === '0') continue;

            tokens.push({ mint, balance: rawBalance });
        }

        console.error('Found ' + tokens.length + ' non-zero token account(s)');
        for (const t of tokens) {
            console.error('  ' + t.mint + ' balance=' + t.balance);
        }

        console.log(JSON.stringify({ success: true, tokens }));
    } catch (err) {
        console.log(JSON.stringify({ success: false, error: err.message }));
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
    case 'swap':
        swapTokens(process.argv[3], parseFloat(process.argv[4]), parseFloat(process.argv[5] || 5), process.argv[6] || 'buy');
        break;
    case 'unwrap':
        unwrapWsol();
        break;
    case 'lpvalue':
        getLpValue(process.argv[3], process.argv[4]);
        break;
    case 'poolkeys':
        testPoolKeys(process.argv[3]);
        break;
    case 'listtokens':
        listTokens();
        break;
    case 'test':
        test();
        break;
    default:
        console.error('Unknown command. Usage:');
        console.error('  node raydium_sdk_bridge.js add <poolId> <amountA> <amountB> [slippage]');
        console.error('  node raydium_sdk_bridge.js remove <poolId> <lpAmount> [slippage]');
        console.error('  node raydium_sdk_bridge.js balance <tokenMint>');
        console.error('  node raydium_sdk_bridge.js listtokens');
        console.error('  node raydium_sdk_bridge.js poolkeys <poolId>');
        console.error('  node raydium_sdk_bridge.js test');
        process.exit(1);
}
