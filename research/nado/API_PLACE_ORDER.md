# Nado API - Place Order Reference

**Source**: https://docs.nado.xyz/developer-resources/api/gateway/executes/place-order

## Request

```
POST [GATEWAY_REST_ENDPOINT]/execute
```

```json
{
  "place_order": {
    "product_id": 4,
    "order": {
      "sender": "0x7a5ec2748e9065794491a8d29dcf3f9edb8d7c43746573743000000000000000",
      "priceX18": "3000000000000000000000",
      "amount": "10000000000000000",
      "expiration": "4294967295",
      "nonce": "1757062078359666688",
      "appendix": "1"
    },
    "signature": "0x...",
    "id": 100
  }
}
```

## Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| product_id | number | Yes | Product ID (4=ETH, 2=BTC, etc) |
| order.sender | string | Yes | 32-byte hex: address + subaccount name |
| order.priceX18 | string | Yes | Price * 1e18 |
| order.amount | string | Yes | Quantity * 1e18. Positive=buy, negative=sell |
| order.expiration | string | Yes | Unix timestamp (seconds) when order expires |
| order.nonce | string | Yes | Encodes recv_time + random bits |
| order.appendix | string | Yes | Order type, reduce_only, etc. |
| signature | string | Yes | EIP-712 signature |
| spot_leverage | boolean | No | Use leverage (default: true) |

## Order Nonce Format

The nonce encodes two pieces of information:
- **Most significant 44 bits**: `recv_time` (ms) - time after which order is discarded by matching engine
- **Least significant 20 bits**: random integer to avoid hash collisions

```python
import time
recv_time_ms = int(time.time() * 1000) + 30000  # 30 seconds from now
random_bits = random.getrandbits(20)
nonce = (recv_time_ms << 20) + random_bits
```

## Order Appendix Bit Layout

```
| value   | reserved | trigger | reduce_only | order_type | isolated | version |
| 64 bits | 50 bits  | 2 bits  | 1 bit       | 2 bits     | 1 bit    | 8 bits  |
```

- **version** (bits 0-7): protocol version = 1
- **isolated** (bit 8): isolated margin
- **order_type** (bits 9-10): 0=DEFAULT, 1=IOC, 2=FOK, 3=POST_ONLY
- **reduce_only** (bit 11): only reduce position

## Signing Notes

**CRITICAL**: For place_order, the verifying contract = `address(productId)`

```python
def gen_order_verifying_contract(product_id: int) -> str:
    be_bytes = product_id.to_bytes(20, byteorder="big", signed=False)
    return "0x" + be_bytes.hex()

# Example: product_id 4 (ETH) = 0x0000000000000000000000000000000000000004
```

## Price Requirements

- Price must be within 20% to 500% of oracle price
- Price must be divisible by `price_increment_x18` for the product

## Response

Success:
```json
{
  "status": "success",
  "signature": "0x...",
  "data": {
    "digest": "0x..."
  },
  "request_type": "execute_place_order"
}
```

Error:
```json
{
  "status": "failure",
  "signature": "0x...",
  "error": "error message",
  "error_code": 2007,
  "request_type": "execute_place_order"
}
```

## Common Error Codes

- 2000: Invalid order price (not divisible by price_increment)
- 2007: Order price out of range (20%-500% of oracle)
- 2011: Request received after recv_time (nonce issue)
