# Nado API Signing Reference

**Source**: https://docs.nado.xyz/developer-resources/api/gateway/signing

## Domain

```json
{
    "name": "Nado",
    "version": "0.0.1",
    "chainId": chainId,
    "verifyingContract": contractAddress
}
```

**CRITICAL**: The verifying contract varies by execute type:
- **For place order**: Use `address(productId)` - the 20 bytes hex representation of the productId
  - Example: product_id 18 = `0x0000000000000000000000000000000000000012`
  - Example: product_id 4 (ETH) = `0x0000000000000000000000000000000000000004`
- **For everything else**: Use the endpoint address from `/query?type=contracts`

```python
def gen_order_verifying_contract(product_id: int) -> str:
    """Generate the order verifying contract address from product ID."""
    be_bytes = product_id.to_bytes(20, byteorder="big", signed=False)
    return "0x" + be_bytes.hex()
```

## EIP712 Types

### Place Order
**Primary Type**: `Order`

```solidity
struct Order {
    bytes32 sender;
    int128 priceX18;
    int128 amount;
    uint64 expiration;
    uint64 nonce;
    uint128 appendix;
}
```

```json
{
  "Order": [
    { "name": "sender", "type": "bytes32" },
    { "name": "priceX18", "type": "int128" },
    { "name": "amount", "type": "int128" },
    { "name": "expiration", "type": "uint64" },
    { "name": "nonce", "type": "uint64" },
    { "name": "appendix", "type": "uint128" }
  ]
}
```

### Cancel Orders
**Primary Type**: `Cancellation`

```solidity
struct Cancellation {
    bytes32 sender;
    uint32[] productIds;
    bytes32[] digests;
    uint64 nonce;
}
```

### Link Signer
**Primary Type**: `LinkSigner`

```solidity
struct LinkSigner {
    bytes32 sender;
    bytes32 signer;
    uint64 nonce;
}
```

## Chain Info
- **Mainnet Chain ID**: 57073 (Ink)
- **Testnet Chain ID**: 763373 (Ink Sepolia)
- **Endpoint Address**: Get from `/query?type=contracts`
