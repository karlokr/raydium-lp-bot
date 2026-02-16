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
const RAYDIUM_V4_PROGRAM = new PublicKey('675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8');

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

/**
 * Retry wrapper for RPC calls that can fail with transient network errors
 * (e.g. "TypeError: fetch failed" from the Helius endpoint).
 * Retries up to `maxRetries` times with exponential backoff.
 */
async function rpcRetry(fn, label = 'RPC call', maxRetries = 3) {
    for (let attempt = 1; attempt <= maxRetries; attempt++) {
        try {
            return await fn();
        } catch (err) {
            const msg = err?.message || String(err);
            const isTransient = msg.includes('fetch failed') ||
                                msg.includes('FetchError') ||
                                msg.includes('ECONNRESET') ||
                                msg.includes('ETIMEDOUT') ||
                                msg.includes('socket hang up') ||
                                msg.includes('503') ||
                                msg.includes('429');
            if (!isTransient || attempt === maxRetries) {
                throw err;
            }
            const delay = attempt * 2000;  // 2s, 4s, 6s
            console.error(`[RPC] ${label} failed (attempt ${attempt}/${maxRetries}): ${msg} — retrying in ${delay}ms...`);
            await new Promise(r => setTimeout(r, delay));
        }
    }
}

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
    const ammAccountInfo = await rpcRetry(
        () => connection.getAccountInfo(poolPubkey),
        'getAccountInfo(AMM ' + poolId.slice(0, 8) + ')'
    );
    if (!ammAccountInfo) {
        throw new Error('AMM account ' + poolId + ' not found on-chain');
    }
    
    const ammProgramId = ammAccountInfo.owner;
    console.error('AMM program owner: ' + ammProgramId.toString());
    
    // Only Raydium V4 AMM is supported. CPMM (CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C)
    // and CLMM pools have completely different binary layouts and will produce
    // garbage data if decoded with LIQUIDITY_STATE_LAYOUT_V4.
    if (!ammProgramId.equals(RAYDIUM_V4_PROGRAM)) {
        throw new Error('Unsupported pool program: ' + ammProgramId.toString() + '. Only Raydium V4 AMM (675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8) is supported.');
    }
    
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
    const marketAccountInfo = await rpcRetry(
        () => connection.getAccountInfo(marketId),
        'getAccountInfo(Market ' + marketId.toString().slice(0, 8) + ')'
    );
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
        // Build a minimal accountInfo object using raw buffer reads to avoid
        // SPL_ACCOUNT_LAYOUT.decode() which can throw "53 bits" on large amounts.
        // SPL Token account layout:
        //   offset 0:  mint (32 bytes, PublicKey)
        //   offset 32: owner (32 bytes, PublicKey)
        //   offset 64: amount (8 bytes, u64 LE)
        const data = account.data;
        const mint = new PublicKey(data.subarray(0, 32));
        const owner = new PublicKey(data.subarray(32, 64));
        const amount = new BN(data.readBigUInt64LE(64).toString());
        accounts.push({
            pubkey,
            programId: TOKEN_PROGRAM_ID,
            accountInfo: { mint, owner, amount },
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
        // Read amount from raw buffer (offset 64, u64 LE) to avoid 53-bit overflow
        const actualNonSolRaw = new BN(nonSolAcctInfo.data.readBigUInt64LE(64).toString());
        const actualNonSol = parseFloat(new Decimal(actualNonSolRaw.toString()).div(new Decimal(10).pow(nonSolDecimals)).toString());
        
        console.error('Actual non-SOL token balance: ' + actualNonSol + ' (raw: ' + actualNonSolRaw.toString() + ')');
        
        // Read pool vaults AND OpenOrders account to compute effective reserves.
        // The AMM on-chain uses: effectiveReserve = vaultAmount + openOrdersTotal
        // Using only vault amounts causes 0x1e slippage because the ratio is wrong.
        const [baseVaultInfo, quoteVaultInfo, openOrdersInfo, lpMintInfo] = await rpcRetry(
            () => Promise.all([
                connection.getAccountInfo(poolKeys.baseVault),
                connection.getAccountInfo(poolKeys.quoteVault),
                connection.getAccountInfo(poolKeys.openOrders),
                connection.getAccountInfo(poolKeys.lpMint),
            ]),
            'getAccountInfo(vaults+openOrders) for addLiquidity'
        );
        if (!baseVaultInfo || !quoteVaultInfo) {
            throw new Error('Could not read pool vault accounts');
        }
        // Read amounts from raw buffer (offset 64, u64 LE) to avoid 53-bit overflow
        const baseVaultAmount = new BN(baseVaultInfo.data.readBigUInt64LE(64).toString());
        const quoteVaultAmount = new BN(quoteVaultInfo.data.readBigUInt64LE(64).toString());
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
        let wsolAmountRaw = baseIsWsol ? amountInA.raw : amountInB.raw;
        const wsolAta = baseIsWsol ? baseAta : quoteAta;
        let wrapLamports = BigInt(wsolAmountRaw.toString());
        const overhead = BigInt(7_000_000); // rent + fees
        let totalNeeded = wrapLamports + overhead;
        let nativeBal = BigInt(await connection.getBalance(wallet.publicKey));
        
        console.error('SDK needs ' + totalNeeded.toString() + ' native lamports, have ' + nativeBal.toString());
        
        if (nativeBal < totalNeeded) {
            // Try unwrapping any existing WSOL first
            const wsolAcctInfo = await connection.getAccountInfo(wsolAta);
            if (wsolAcctInfo) {
                const wsolBalance = wsolAcctInfo.data.readBigUInt64LE(64);
                console.error('Unwrapping WSOL ATA (' + wsolBalance.toString() + ' lamports) to get native SOL...');
                const unwrapTx = new Transaction();
                unwrapTx.add(createCloseAccountInstruction(wsolAta, wallet.publicKey, wallet.publicKey));
                const { blockhash: ub } = await connection.getLatestBlockhash();
                unwrapTx.recentBlockhash = ub;
                unwrapTx.feePayer = wallet.publicKey;
                const us = await connection.sendTransaction(unwrapTx, [wallet]);
                await connection.confirmTransaction(us, 'confirmed');
                console.error('WSOL unwrapped: ' + us);
                nativeBal = BigInt(await connection.getBalance(wallet.publicKey));
            }
            
            // If still insufficient, re-fix on the SOL side instead of the token side.
            // This happens when the wallet holds leftover tokens from a failed exit swap,
            // making the total token balance larger than what this entry intended.
            if (nativeBal < totalNeeded) {
                console.error('Still insufficient SOL after unwrap (' + nativeBal.toString() + ' < ' + totalNeeded.toString() + ')');
                console.error('Re-computing: fixing on SOL side to match available balance...');
                
                const usableLamports = nativeBal - overhead;
                if (usableLamports <= BigInt(0)) {
                    throw new Error('Not enough SOL for add liquidity (need ' + totalNeeded.toString() + ' lamports, have ' + nativeBal.toString() + ')');
                }
                
                const solToken = baseIsWsol ? baseToken : quoteToken;
                const solFixedAmount = new TokenAmount(solToken, new BN(usableLamports.toString()), true);
                const solFixedSide = baseIsWsol ? 'a' : 'b';
                
                const recomputed = Liquidity.computeAnotherAmount({
                    poolKeys,
                    poolInfo,
                    amount: solFixedAmount,
                    anotherCurrency: baseIsWsol ? quoteToken : baseToken,
                    slippage: slippagePercent,
                });
                
                if (baseIsWsol) {
                    amountInA = solFixedAmount;            // SOL (capped to what we have)
                    amountInB = recomputed.maxAnotherAmount;  // token (computed from SOL)
                    fixedSide = 'a';
                } else {
                    amountInA = recomputed.maxAnotherAmount;  // token (computed from SOL)
                    amountInB = solFixedAmount;            // SOL (capped to what we have)
                    fixedSide = 'b';
                }
                
                console.error('Recomputed - amountInA: ' + amountInA.raw.toString() + ', amountInB: ' + amountInB.raw.toString() + ', fixedSide: ' + fixedSide);
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
        // Read amount from raw buffer (offset 64, u64 LE) to avoid 53-bit overflow
        const rawAmount = new BN(lpAccountInfo.data.readBigUInt64LE(64).toString());
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
        
        // Read amount from raw buffer (offset 64, u64 LE) to avoid 53-bit overflow
        const balance = accountInfo.data.readBigUInt64LE(64).toString();
        
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
                // Read amount directly from raw buffer (offset 64, u64 LE) to avoid
                // SPL_ACCOUNT_LAYOUT.decode which can throw on >53 bit amounts
                const rawBalance = new BN(acctInfo.data.readBigUInt64LE(64).toString());
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
        
        // Step 5: Verify sufficient funds
        if (inputIsWsol) {
            console.error('[5/7] Checking native SOL balance for swap...');
            const wrapLamports = BigInt(amountInRaw.toString());
            const feesReserve = BigInt(5_000_000); // 0.005 SOL for tx fees
            const totalNeeded = wrapLamports + feesReserve;
            
            const nativeBal = BigInt(await connection.getBalance(wallet.publicKey));
            console.error('  Native SOL: ' + nativeBal.toString() + ' lamports');
            console.error('  Need: ' + totalNeeded.toString() + ' lamports (amount + fees)');
            
            if (nativeBal < totalNeeded) {
                // Try unwrapping WSOL ATA to free native SOL
                const wsolAta = baseIsWsol ? baseAta : quoteAta;
                const wsolAcctInfo = await connection.getAccountInfo(wsolAta);
                if (wsolAcctInfo) {
                    const wsolBalance = wsolAcctInfo.data.readBigUInt64LE(64);
                    const rentRefund = BigInt(2_039_280);
                    if (nativeBal + wsolBalance + rentRefund >= totalNeeded) {
                        console.error('  Unwrapping WSOL ATA (' + wsolBalance.toString() + ' lamports)...');
                        const unwrapTx = new Transaction();
                        unwrapTx.add(createCloseAccountInstruction(wsolAta, wallet.publicKey, wallet.publicKey));
                        const { blockhash: ub } = await connection.getLatestBlockhash();
                        unwrapTx.recentBlockhash = ub;
                        unwrapTx.feePayer = wallet.publicKey;
                        const unwrapSig = await connection.sendTransaction(unwrapTx, [wallet]);
                        await connection.confirmTransaction(unwrapSig, 'confirmed');
                        console.error('  WSOL unwrapped: ' + unwrapSig);
                    } else {
                        const have = Number(nativeBal + wsolBalance) / 1e9;
                        const need = Number(totalNeeded) / 1e9;
                        throw new Error('Insufficient funds. Have ' + have.toFixed(4) + ' SOL total but need ' + need.toFixed(4) + ' SOL');
                    }
                } else {
                    const have = (Number(nativeBal) / 1e9).toFixed(4);
                    const need = (Number(totalNeeded) / 1e9).toFixed(4);
                    throw new Error('Insufficient native SOL. Have ' + have + ' SOL but need ' + need + ' SOL');
                }
            } else {
                console.error('  Sufficient native SOL');
            }
        } else {
            console.error('[5/7] Input is not WSOL, skipping wrap');
            
            // Verify the input token account has enough balance
            const inputAta = inputToken.mint.equals(poolKeys.baseMint) ? baseAta : quoteAta;
            const inputAcctInfo = await connection.getAccountInfo(inputAta);
            if (!inputAcctInfo) {
                throw new Error('Input token account does not exist: ' + inputAta.toString());
            }
            // Read amount from raw buffer (offset 64, u64 LE) to avoid 53-bit overflow
            const inputBalance = new BN(inputAcctInfo.data.readBigUInt64LE(64).toString());
            console.error('  Input token balance: ' + inputBalance.toString() + ' raw');
            if (inputBalance.lt(amountInRaw)) {
                throw new Error('Insufficient input token balance. Have ' + inputBalance.toString() + ' but need ' + amountInRaw.toString());
            }
        }
        
        // Step 6: Execute swap via SDK
        console.error('[6/7] Building swap instruction...');
        
        // Read vault balances + OpenOrders to compute effective reserves
        // (matches what the AMM program uses internally)
        //
        // IMPORTANT: Read vault amounts from raw buffer using BigInt, NOT via
        // SPL_ACCOUNT_LAYOUT.decode. The SDK's layout decoder calls BN.toNumber()
        // on the u64 amount field, which throws "Number can only safely store up
        // to 53 bits" when a vault holds >9M tokens at 9 decimals.
        const [baseVaultInfo, quoteVaultInfo, openOrdersInfo, lpMintInfo] = await rpcRetry(
            () => Promise.all([
                connection.getAccountInfo(poolKeys.baseVault),
                connection.getAccountInfo(poolKeys.quoteVault),
                connection.getAccountInfo(poolKeys.openOrders),
                connection.getAccountInfo(poolKeys.lpMint),
            ]),
            'getAccountInfo(vaults+openOrders) for swap'
        );
        if (!baseVaultInfo || !quoteVaultInfo) {
            throw new Error('Could not read vault accounts for pool reserves');
        }
        // SPL Token account layout: amount is a u64 LE at offset 64
        const baseVaultAmount = new BN(baseVaultInfo.data.readBigUInt64LE(64).toString());
        const quoteVaultAmount = new BN(quoteVaultInfo.data.readBigUInt64LE(64).toString());
        
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
        
        // Compute amounts first to catch errors early.
        // The SDK's computeAmountOut internally calls BN.toNumber() which throws
        // "Number can only safely store up to 53 bits" when pool reserves exceed
        // 2^53 raw units (e.g. tokens with 9 decimals and >9M supply).
        // Fallback: compute manually using Decimal (arbitrary precision).
        let amountOut, minAmountOut;
        try {
            const computed = Liquidity.computeAmountOut({
                poolKeys,
                poolInfo,
                amountIn: amountInToken,
                currencyOut: outputToken,
                slippage: slippagePercent,
            });
            amountOut = computed.amountOut;
            minAmountOut = computed.minAmountOut;
        } catch (sdkErr) {
            if (!sdkErr.message.includes('53 bit')) throw sdkErr;
            console.error('  SDK computeAmountOut overflow — using Decimal fallback');

            // Raydium V4 constant-product AMM: fee = 25 bps (0.25%)
            const FEE_NUMERATOR = 25;
            const FEE_DENOMINATOR = 10000;

            // Determine input/output reserves
            const inputIsBase = inputToken.mint.equals(poolKeys.baseMint);
            const inReserve  = new Decimal((inputIsBase ? baseReserve : quoteReserve).toString());
            const outReserve = new Decimal((inputIsBase ? quoteReserve : baseReserve).toString());

            const amountInDec  = new Decimal(amountInRaw.toString());
            const amountInFee  = amountInDec.mul(FEE_DENOMINATOR - FEE_NUMERATOR).div(FEE_DENOMINATOR);
            const numerator    = amountInFee.mul(outReserve);
            const denominator  = inReserve.add(amountInFee);
            const amountOutRaw = numerator.div(denominator).floor();

            // Apply slippage
            const slippageMul  = new Decimal(1).minus(new Decimal(slippage).div(100));
            const minOutRaw    = amountOutRaw.mul(slippageMul).floor();

            amountOut    = new TokenAmount(outputToken, new BN(amountOutRaw.toFixed(0)), true);
            minAmountOut = new TokenAmount(outputToken, new BN(minOutRaw.toFixed(0)), true);
        }
        
        console.error('  Expected out: ' + amountOut.raw.toString() + ' raw, Min out: ' + minAmountOut.raw.toString() + ' raw');
        
        // Use makeSwapInstruction (NOT makeSwapInstructionSimple) because the
        // Simple version internally re-calls computeAmountOut which hits the
        // same 53-bit overflow on pools with large reserves.
        // makeSwapInstruction just builds the instruction from our pre-computed values.
        const swapIxs = Liquidity.makeSwapInstruction({
            poolKeys,
            userKeys: {
                tokenAccountIn: inputToken.mint.equals(poolKeys.baseMint) ? baseAta : quoteAta,
                tokenAccountOut: outputToken.mint.equals(poolKeys.baseMint) ? baseAta : quoteAta,
                owner: wallet.publicKey,
            },
            amountIn: amountInRaw,
            amountOut: minAmountOut.raw,
            fixedSide: 'in',
        });
        
        // Step 7: Build and send transaction
        // makeSwapInstruction (non-Simple) does NOT handle WSOL wrapping.
        // We must manually wrap SOL → WSOL before the swap (buy direction)
        // and unwrap WSOL → SOL after the swap (sell direction).
        console.error('[7/7] Building and sending swap transaction...');
        const txids = [];
        
        // makeSwapInstruction returns { innerTransaction } with instructions + signers
        const innerTx = swapIxs.innerTransaction || swapIxs;
        const swapInstructions = innerTx.instructions || [];
        
        if (swapInstructions.length === 0) {
            throw new Error('makeSwapInstruction returned no instructions');
        }
        
        const tx = new Transaction();
        const wsolAta = baseIsWsol ? baseAta : quoteAta;
        
        // Pre-swap: if buying with SOL, wrap native SOL into WSOL ATA
        if (inputIsWsol) {
            console.error('  Wrapping ' + amountInRaw.toString() + ' lamports into WSOL ATA...');
            // WSOL ATA already exists (created by getOrCreateTokenAccount above).
            // Transfer SOL into the WSOL ATA and sync to update its balance.
            tx.add(
                SystemProgram.transfer({
                    fromPubkey: wallet.publicKey,
                    toPubkey: wsolAta,
                    lamports: BigInt(amountInRaw.toString()),
                })
            );
            // Sync native — makes the ATA reflect the transferred SOL as WSOL balance
            tx.add(createSyncNativeInstruction(wsolAta));
        }
        
        // Add the actual swap instruction(s)
        tx.add(...swapInstructions);
        
        // Post-swap: unwrap WSOL ATA back to native SOL
        // For buys: close WSOL ATA to reclaim any dust + rent
        // For sells: close WSOL ATA to convert received WSOL to native SOL
        tx.add(
            createCloseAccountInstruction(wsolAta, wallet.publicKey, wallet.publicKey)
        );
        
        const { blockhash } = await connection.getLatestBlockhash();
        tx.recentBlockhash = blockhash;
        tx.feePayer = wallet.publicKey;
        
        const signers = [wallet];
        if (innerTx.signers && innerTx.signers.length > 0) {
            signers.push(...innerTx.signers);
        }
        
        const signature = await connection.sendTransaction(tx, signers);
        await connection.confirmTransaction(signature, 'confirmed');
        txids.push(signature);
        console.error('  Swap confirmed: ' + signature);
        
        console.error('=== SWAP SUCCESS ===');
        console.log(JSON.stringify({
            success: true,
            signatures: txids,
            amountOut: amountOut.raw.toString(),
            minAmountOut: minAmountOut.raw.toString(),
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
        // Read amount from raw buffer (offset 64, u64 LE) to avoid 53-bit overflow
        const balance = acctInfo.data.readBigUInt64LE(64);
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
        const lpAcctInfo = await rpcRetry(
            () => connection.getAccountInfo(lpAta),
            'getAccountInfo(LP ATA)'
        );
        if (!lpAcctInfo) {
            console.log(JSON.stringify({ valueSol: 0, lpBalance: 0 }));
            return;
        }
        // Read amount from raw buffer (offset 64, u64 LE) to avoid 53-bit overflow
        const lpBalance = new BN(lpAcctInfo.data.readBigUInt64LE(64).toString());
        
        if (lpBalance.isZero()) {
            console.log(JSON.stringify({ valueSol: 0, lpBalance: 0 }));
            return;
        }
        
        // Read pool reserves + OpenOrders
        const [baseVaultInfo, quoteVaultInfo, openOrdersInfo] = await rpcRetry(
            () => Promise.all([
                connection.getAccountInfo(poolKeys.baseVault),
                connection.getAccountInfo(poolKeys.quoteVault),
                connection.getAccountInfo(poolKeys.openOrders),
            ]),
            'getAccountInfo(vaults+openOrders) for lpvalue'
        );
        
        // Read amounts from raw buffer (offset 64, u64 LE) to avoid 53-bit overflow
        const baseVault = new BN(baseVaultInfo.data.readBigUInt64LE(64).toString());
        const quoteVault = new BN(quoteVaultInfo.data.readBigUInt64LE(64).toString());
        
        let ooBase = new BN(0);
        let ooQuote = new BN(0);
        if (openOrdersInfo && openOrdersInfo.data.length >= 109) {
            ooBase = new BN(openOrdersInfo.data.readBigUInt64LE(85).toString());
            ooQuote = new BN(openOrdersInfo.data.readBigUInt64LE(101).toString());
        }
        
        // Effective reserves = vault + openOrders - needTakePnl
        // Guard against negative if needTakePnl > vault (stale AMM state)
        const baseReserveRaw = baseVault.add(ooBase);
        const quoteReserveRaw = quoteVault.add(ooQuote);
        const baseReserve = baseReserveRaw.gt(poolKeys.baseNeedTakePnl)
            ? baseReserveRaw.sub(poolKeys.baseNeedTakePnl) : baseReserveRaw;
        const quoteReserve = quoteReserveRaw.gt(poolKeys.quoteNeedTakePnl)
            ? quoteReserveRaw.sub(poolKeys.quoteNeedTakePnl) : quoteReserveRaw;
        
        // Use lpReserve from AMM state as the true circulating LP supply.
        // The raw mint supply is wrong because burned LP tokens reduce it below
        // the AMM's internal tracking. The AMM program uses lpReserve as the
        // denominator when computing LP share of reserves.
        const lpSupply = poolKeys.lpReserve;
        
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
        let valueLamports;
        if (baseIsWsol) {
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
 * Batch LP value lookup for multiple positions.
 *
 * Uses getMultipleAccountsInfo to fetch all accounts in just 2 RPC calls
 * (1 for AMM states, 1 for LP ATAs + vault accounts) instead of 6 per position.
 *
 * Input: JSON string of [{poolId, lpMint}, ...]
 * Output: JSON {results: {poolId: {valueSol, lpBalance, priceRatio}, ...}}
 */
async function batchLpValue(jsonInput) {
    try {
        const entries = JSON.parse(jsonInput);
        if (!entries || !entries.length) {
            console.log(JSON.stringify({ results: {} }));
            return;
        }

        // Step 1: Batch-read all AMM accounts in a single RPC call
        const ammPubkeys = entries.map(e => new PublicKey(e.poolId));
        const ammAccounts = await rpcRetry(
            () => connection.getMultipleAccountsInfo(ammPubkeys),
            'getMultipleAccountsInfo(AMM×' + entries.length + ')'
        );

        // Parse AMM states and collect addresses for step 2
        const poolStates = [];     // one per entry (null if failed)
        const step2Keys = [];      // flat list of pubkeys for batch read
        // For each valid pool: LP ATA, baseVault, quoteVault, openOrders (4 accounts)

        for (let i = 0; i < entries.length; i++) {
            const ammInfo = ammAccounts[i];
            if (!ammInfo || !ammInfo.owner.equals(RAYDIUM_V4_PROGRAM)) {
                poolStates.push(null);
                continue;
            }

            const amm = LIQUIDITY_STATE_LAYOUT_V4.decode(ammInfo.data);
            const baseIsWsol = amm.baseMint.equals(WSOL_MINT);
            const quoteIsWsol = amm.quoteMint.equals(WSOL_MINT);
            if (!baseIsWsol && !quoteIsWsol) {
                poolStates.push(null);
                continue;
            }

            const lpMintPubkey = new PublicKey(entries[i].lpMint);
            const lpAta = await getAssociatedTokenAddress(lpMintPubkey, wallet.publicKey);

            const startIdx = step2Keys.length;
            step2Keys.push(lpAta);           // +0
            step2Keys.push(amm.baseVault);   // +1
            step2Keys.push(amm.quoteVault);  // +2
            step2Keys.push(amm.openOrders);  // +3

            poolStates.push({
                startIdx,
                baseIsWsol,
                baseDecimals: amm.baseDecimal.toNumber(),
                quoteDecimals: amm.quoteDecimal.toNumber(),
                lpReserve: amm.lpReserve,
                baseNeedTakePnl: amm.baseNeedTakePnl,
                quoteNeedTakePnl: amm.quoteNeedTakePnl,
            });
        }

        // Step 2: Batch-read all LP ATAs + vault accounts in a single RPC call
        let step2Accounts = [];
        if (step2Keys.length > 0) {
            step2Accounts = await rpcRetry(
                () => connection.getMultipleAccountsInfo(step2Keys),
                'getMultipleAccountsInfo(vaults×' + step2Keys.length + ')'
            );
        }

        // Step 3: Compute values for each position
        const results = {};

        for (let i = 0; i < entries.length; i++) {
            const poolId = entries[i].poolId;
            const ps = poolStates[i];

            if (!ps) {
                results[poolId] = { valueSol: 0, lpBalance: 0, priceRatio: 0 };
                continue;
            }

            const lpAcctInfo    = step2Accounts[ps.startIdx];
            const baseVaultInfo = step2Accounts[ps.startIdx + 1];
            const quoteVaultInfo = step2Accounts[ps.startIdx + 2];
            const openOrdersInfo = step2Accounts[ps.startIdx + 3];

            if (!lpAcctInfo) {
                results[poolId] = { valueSol: 0, lpBalance: 0, priceRatio: 0 };
                continue;
            }

            const lpBalance = new BN(lpAcctInfo.data.readBigUInt64LE(64).toString());
            if (lpBalance.isZero()) {
                results[poolId] = { valueSol: 0, lpBalance: 0, priceRatio: 0 };
                continue;
            }

            if (!baseVaultInfo || !quoteVaultInfo) {
                results[poolId] = { valueSol: 0, lpBalance: parseInt(lpBalance.toString()), priceRatio: 0 };
                continue;
            }

            const baseVault = new BN(baseVaultInfo.data.readBigUInt64LE(64).toString());
            const quoteVault = new BN(quoteVaultInfo.data.readBigUInt64LE(64).toString());

            let ooBase = new BN(0);
            let ooQuote = new BN(0);
            if (openOrdersInfo && openOrdersInfo.data.length >= 109) {
                ooBase = new BN(openOrdersInfo.data.readBigUInt64LE(85).toString());
                ooQuote = new BN(openOrdersInfo.data.readBigUInt64LE(101).toString());
            }

            const baseReserveRaw = baseVault.add(ooBase);
            const quoteReserveRaw = quoteVault.add(ooQuote);
            const baseReserve = baseReserveRaw.gt(ps.baseNeedTakePnl)
                ? baseReserveRaw.sub(ps.baseNeedTakePnl) : baseReserveRaw;
            const quoteReserve = quoteReserveRaw.gt(ps.quoteNeedTakePnl)
                ? quoteReserveRaw.sub(ps.quoteNeedTakePnl) : quoteReserveRaw;

            const lpSupply = ps.lpReserve;
            if (lpSupply.isZero()) {
                results[poolId] = { valueSol: 0, lpBalance: parseInt(lpBalance.toString()), priceRatio: 0 };
                continue;
            }

            const shareBase = lpBalance.mul(baseReserve).div(lpSupply);
            const shareQuote = lpBalance.mul(quoteReserve).div(lpSupply);

            let valueLamports;
            if (ps.baseIsWsol) {
                const quoteInSol = shareQuote.mul(baseReserve).div(quoteReserve);
                valueLamports = shareBase.add(quoteInSol);
            } else {
                const baseInSol = shareBase.mul(quoteReserve).div(baseReserve);
                valueLamports = shareQuote.add(baseInSol);
            }

            const valueSol = parseFloat(new Decimal(valueLamports.toString()).div(1e9).toString());

            const priceRatio = baseReserve.isZero() ? 0 : parseFloat(
                new Decimal(quoteReserve.toString())
                    .div(new Decimal(10).pow(ps.quoteDecimals))
                    .div(new Decimal(baseReserve.toString()).div(new Decimal(10).pow(ps.baseDecimals)))
                    .toString()
            );

            results[poolId] = {
                valueSol,
                lpBalance: parseInt(lpBalance.toString()),
                priceRatio,
            };
        }

        console.log(JSON.stringify({ results }));

    } catch (err) {
        console.log(JSON.stringify({
            success: false,
            error: err.message,
        }));
        process.exit(1);
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

/**
 * Close all empty (zero-balance) token accounts to reclaim rent.
 * Batches into transactions of up to 20 close instructions each.
 * Optionally accepts a comma-separated list of mints to keep.
 */
async function closeEmptyAccounts(keepMints = '') {
    try {
        const keepSet = new Set(keepMints ? keepMints.split(',').map(m => m.trim()) : []);
        // Always keep WSOL — it's handled separately by unwrap
        keepSet.add(NATIVE_MINT.toString());

        const tokenAccounts = await getOwnerTokenAccounts();
        const toClose = [];
        for (const acct of tokenAccounts) {
            const mint = acct.accountInfo.mint.toString();
            const balance = acct.accountInfo.amount;
            if (balance.isZero() && !keepSet.has(mint)) {
                toClose.push(acct.pubkey);
            }
        }

        if (toClose.length === 0) {
            console.log(JSON.stringify({ success: true, closed: 0, reclaimedSol: 0 }));
            return;
        }

        console.error(`Closing ${toClose.length} empty token account(s)...`);

        let totalClosed = 0;
        const BATCH_SIZE = 20; // ~20 close instructions fit in one tx
        for (let i = 0; i < toClose.length; i += BATCH_SIZE) {
            const batch = toClose.slice(i, i + BATCH_SIZE);
            const tx = new Transaction();
            for (const pubkey of batch) {
                tx.add(createCloseAccountInstruction(pubkey, wallet.publicKey, wallet.publicKey));
            }

            const { blockhash } = await connection.getLatestBlockhash();
            tx.recentBlockhash = blockhash;
            tx.feePayer = wallet.publicKey;
            tx.sign(wallet);
            await connection.sendRawTransaction(tx.serialize(), { skipPreflight: true });
            totalClosed += batch.length;
            console.error(`  Batch ${Math.floor(i / BATCH_SIZE) + 1}: closed ${batch.length} account(s)`);
        }

        // Rent per ATA is ~0.00203928 SOL
        const reclaimedSol = parseFloat((totalClosed * 0.00203928).toFixed(6));
        console.log(JSON.stringify({ success: true, closed: totalClosed, reclaimedSol }));
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
    case 'batchlpvalue':
        batchLpValue(process.argv[3]);
        break;
    case 'poolkeys':
        testPoolKeys(process.argv[3]);
        break;
    case 'listtokens':
        listTokens();
        break;
    case 'closeaccounts':
        closeEmptyAccounts(process.argv[3] || '');
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
