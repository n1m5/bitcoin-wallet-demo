# bitcoin-wallet-demo
Demo of a Bitcoin testnet wallet paying in a fake snack shop.

## Flow
1. **Import wallet** — paste a testnet WIF private key (and derivation path). Balance + UTXOs are fetched live from mempool.space.
2. **Shop** — add snacks (fixed 10,000 sats each). Other items are shown as sold out.
3. **Cart + Checkout** — review your selection. Click "Proceed to Checkout".
4. **Pay in Bitcoin** — the app builds, signs and broadcasts a real testnet transaction from your imported wallet to the demo merchant address.
5. **Transaction Journey** — educational breakdown of what happened (UTXO selection, SegWit construction, signing, broadcast, mempool, mining, confirmations) + live confirmation tracker (polls until 5 confirmations).

Everything uses Bitcoin testnet. No real value moves.

Run:
```bash
python app.py
```
Open http://127.0.0.1:5001 (or the PORT you set).

Fund the address via any testnet faucet if your balance is zero.
