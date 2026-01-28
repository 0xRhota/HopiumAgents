#!/usr/bin/env python3
"""
Generate a Linked Signer key for Nado trading

The linked signer is a random private key that you authorize to sign trades.
It never holds any funds - your main wallet keeps all the money.

Usage:
1. Run this script to generate a new key pair
2. Copy the address and paste it into Nado UI "1-Click Trading" setup
3. Copy the private key to your .env file as NADO_LINKED_SIGNER_PRIVATE_KEY
"""

from eth_account import Account

def generate_linked_signer():
    """Generate a new random linked signer key pair"""
    # Generate random account
    account = Account.create()

    print("=" * 60)
    print("  NADO LINKED SIGNER KEY GENERATOR")
    print("=" * 60)
    print()
    print("🔑 Linked Signer Address (paste into Nado UI):")
    print(f"   {account.address}")
    print()
    print("🔒 Linked Signer Private Key (add to .env):")
    print(f"   {account.key.hex()}")
    print()
    print("=" * 60)
    print()
    print("📋 SETUP INSTRUCTIONS:")
    print()
    print("1. Go to app.nado.xyz (or testnet)")
    print("2. Connect your main wallet")
    print("3. Go to Settings → 1-Click Trading")
    print("4. Click 'Enable' and paste the ADDRESS above")
    print("   (NOT the private key - the address!)")
    print("5. Sign the transaction with your main wallet")
    print()
    print("6. Add to your .env file:")
    print(f"   NADO_LINKED_SIGNER_PRIVATE_KEY={account.key.hex()}")
    print()
    print("⚠️  SECURITY NOTES:")
    print("   - This key only has trading permission on YOUR subaccount")
    print("   - It cannot withdraw funds to a different address")
    print("   - If compromised, attacker can only trade (not steal funds)")
    print("   - You can revoke it anytime from the main wallet")
    print()
    return account

if __name__ == "__main__":
    generate_linked_signer()
