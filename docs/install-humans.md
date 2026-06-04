# Install guide (for humans)

If you've never installed a Python CLI tool before, follow these steps in order. Should take 5–10 minutes the first time. After that, using the tool is basically two commands.

## What you're installing

`primecli` is two command-line tools, `deltaprime` and `degenprime`, that let you operate the DeltaPrime (Avalanche) and DegenPrime (Base) DeFi protocols from a terminal. They handle the on-chain plumbing (signing, RedStone oracle payloads, ParaSwap routing, multicall batching) so you can deposit, borrow, swap, etc. with one command each.

**You should know going in:**

- You need a crypto wallet with a private key. The tool signs transactions with that key. You hold the key; the tool just reads it.
- This is real on-chain money. The tool previews every transaction before broadcasting — you confirm with `--execute` after reading the preview. Do not type `--execute` blindly.
- This is community tooling, not an official DeltaPrime / DegenPrime product. The DeltaPrimeLabs team is not affiliated.

## Step 1 — Make sure you have Python 3.10 or newer

Open a terminal and run:

```bash
python3 --version
```

Expected output: `Python 3.10.x` or higher (3.11, 3.12, 3.13 are all fine).

If Python is missing or too old:

- **macOS:** `brew install python@3.12` (install Homebrew first from https://brew.sh if you don't have it).
- **Ubuntu / Debian:** `sudo apt update && sudo apt install python3.12 python3.12-venv`.
- **Windows:** Download the installer from https://www.python.org/downloads/ — check "Add Python to PATH" during install.

## Step 2 — Create a clean Python environment

This keeps `primecli` and its dependencies separate from anything else on your system. Strongly recommended.

```bash
python3 -m venv ~/primecli-env
source ~/primecli-env/bin/activate
```

On Windows (PowerShell), the activation line is `~/primecli-env/Scripts/Activate.ps1` instead.

Your prompt should now show `(primecli-env)` somewhere. That means the environment is active.

## Step 3 — Install primecli

```bash
pip install primecli
```

That's it. You should see a bunch of dependencies installing (web3, eth-account, requests, etc.) and then a confirmation line: `Successfully installed primecli-0.5.x ...`.

Quick check that it worked:

```bash
deltaprime --help | head -5
degenprime --help | head -5
```

Both should print a short docstring describing the tool. If you get "command not found", your venv probably isn't active — run the `source ~/primecli-env/bin/activate` line from Step 2 again.

## Step 4 — Try a read-only command (no key needed yet)

Before configuring your key, confirm the tool can read on-chain state:

```bash
deltaprime pool-info usdc
```

Expected output: a table with Total Supply, Total Borrowed, Utilization, Token Price, TVL for the USDC pool on Avalanche.

Same on Base:

```bash
degenprime pool-info usdc
```

If both work, the install is good.

## Step 5 — Configure your signing key

This is the sensitive part. The tool reads your private key from an environment variable. You have three options, from easiest to most secure:

### Option A — Export in your shell (easy, fine for testing)

```bash
export DELTAPRIME_PRIVATE_KEY=0xabc...your-key-here
```

Lives in your shell's memory until you close the terminal. Re-paste it every time, or add the line to your `~/.bashrc` / `~/.zshrc` (then `source` that file). The key will be visible in `env` dumps and `/proc/<pid>/environ` while it's exported.

### Option B — Key file (more careful)

Put the raw key (just the `0x...` hex string, nothing else) in a file:

```bash
echo "0xabc...your-key-here" > ~/.primecli-key
chmod 600 ~/.primecli-key
export DELTAPRIME_KEY_FILE=~/.primecli-key
```

The `chmod 600` makes the file readable only by you. The env var just points the tool at the file; the key itself stays on disk, not in process env.

### Option C — Per-command flag (most paranoid)

Pass the key per command. Never persisted anywhere:

```bash
deltaprime my-positions --key 0xabc...your-key-here
```

Annoying to type every time but the cleanest from a "where can this key leak from" perspective.

### Where do I get a private key?

If you already have an EVM wallet (MetaMask, Rabby, Frame, a hardware wallet, etc.) you can export the private key from there. Look for an "Export Private Key" option in your wallet's settings. Be aware: a private key gives full control of the wallet. **Never share it.** Never paste it in a chat, an email, an AI assistant, or a screenshot.

If you don't have one and just want to test, you can generate a fresh key with any wallet app. Don't send real funds to it until you're comfortable.

### One key, both chains

The same EVM private key works on Avalanche and Base — they're both EVM chains. So `DELTAPRIME_PRIVATE_KEY` is also picked up by the `degenprime` command if you don't set `DEGENPRIME_PRIVATE_KEY` separately. One key, both tools.

## Step 6 — Try a real read on your account

Once the key is configured:

```bash
deltaprime my-positions
```

Expected output starts with `Wallet: 0x...your-address...`. **Verify that address matches your actual wallet** before you go further — this is the single most important safety check. If it's wrong, your key resolution is wrong.

If your wallet has no DeltaPrime positions yet, you'll see balances of 0 and a note that no Prime Account exists. That's fine.

Same check on Base:

```bash
degenprime my-positions
```

## Step 7 — Make a real transaction

Every state-changing command (`deposit`, `withdraw`, `borrow`, `repay`, `fund`, `swap`, etc.) **previews by default**. You add `--execute` only after reading the preview.

Example: deposit 100 USDC into the Avalanche lending pool.

```bash
# Step 7a — Preview. No money moves.
deltaprime deposit --pool usdc --amount 100

# Reads the preview. Confirms it says what you want.
# If anything looks off (wrong wallet, wrong amount, wrong pool), STOP.

# Step 7b — Broadcast.
deltaprime deposit --pool usdc --amount 100 --execute
```

The preview tells you exactly which contract will be called and how the funds will move. **Always read it before adding `--execute`.**

### Heads-up: lender withdraw is 24h-delayed

Both `deltaprime withdraw` and `degenprime withdraw` register a **withdrawal intent** that becomes executable ~24h later for a 48h window (24h-72h total). You don't get the funds back in one tx. To pull a matured intent: `deltaprime withdrawal-requests` (lists pending intents) → wait for maturity → `deltaprime execute-withdrawal-request --pool <p> --execute`. To cancel before maturity: `deltaprime cancel-withdrawal-request --pool <p> --index N --execute`.

## Common gotchas

- **"command not found: deltaprime"** — your venv isn't active. Run `source ~/primecli-env/bin/activate` again.
- **"No signing key found"** — `DELTAPRIME_PRIVATE_KEY` (or the equivalent file / `--key` flag) is not set. See Step 5.
- **"429 Too Many Requests"** — you're hitting the public RPC limits. Set `DELTAPRIME_RPC` / `DEGENPRIME_RPC` to a paid endpoint like Alchemy or QuickNode. The free tier of either is plenty for personal use.
- **"InvalidExecutor"** on a swap — ParaSwap rotated its executor set faster than the tool's allowlist. The tool tries to auto-patch to a known-good executor. If it persists, [open an issue](https://github.com/Mnemosyne-quest/primecli/issues).
- **Numbers in the preview look wrong** — check the `Wallet:` line first (right account?), then check pool symbol (`usdc` vs `wavax` vs `weth` etc.), then check the amount. The tool never auto-converts units; the number you type is what gets sent.

## Updating to a new version

When a new version ships:

```bash
source ~/primecli-env/bin/activate   # activate the venv
pip install --upgrade primecli
```

Check what changed at https://github.com/Mnemosyne-quest/primecli/releases.

## Where to next

- Quick command tour: [README](../README.md).
- Full command reference, addresses, oracle integration: [docs/deltaprime-reference.md](deltaprime-reference.md) (Avalanche) and [docs/degenprime-reference.md](degenprime-reference.md) (Base).
- Trust model and what the tool does/doesn't protect against: [docs/security.md](security.md).
- Troubleshooting: the "Troubleshooting" section in the [README](../README.md).

## A word on caution

Test with small amounts first. The tool's preview is your safety net — if you've never used DeFi before, deposit $5 of USDC first, watch it appear on the protocol's UI, and only then scale up. There's no support team to call if you broadcast the wrong transaction; the chain doesn't have an undo button.
