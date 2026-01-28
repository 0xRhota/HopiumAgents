#!/usr/bin/env python3
"""
Submit a signed LinkSigner message to Nado API
"""

import sys
import requests

def submit_link_signer(sender: str, signer: str, nonce: str, signature: str, testnet: bool = False):
    """Submit the signed LinkSigner to Nado"""

    gateway = "https://gateway.test.nado.xyz/v1" if testnet else "https://gateway.prod.nado.xyz/v1"

    payload = {
        "type": "link_signer",
        "sender": sender,
        "signer": signer,
        "nonce": nonce,
        "signature": signature
    }

    print(f"Submitting to {gateway}/execute...")
    print(f"Payload: {payload}")

    resp = requests.post(
        f"{gateway}/execute",
        json=payload,
        headers={
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip, deflate"
        }
    )

    print(f"\nResponse status: {resp.status_code}")
    print(f"Response: {resp.text}")

    try:
        result = resp.json()
        if result.get("status") == "success":
            print("\n✓ SUCCESS! Linked signer authorized.")
            print("You can now run: python -m nado_agent.bot_nado --test")
        else:
            print(f"\n✗ FAILED: {result.get('error', result)}")
    except:
        print(f"\nRaw response: {resp.text}")

if __name__ == "__main__":
    if len(sys.argv) < 5:
        print("Usage: python submit_link_signer.py <sender> <signer> <nonce> <signature> [--testnet]")
        print("\nGet these values from the HTML signing page after signing with MetaMask.")
        sys.exit(1)

    sender = sys.argv[1]
    signer = sys.argv[2]
    nonce = sys.argv[3]
    signature = sys.argv[4]
    testnet = "--testnet" in sys.argv

    submit_link_signer(sender, signer, nonce, signature, testnet)
