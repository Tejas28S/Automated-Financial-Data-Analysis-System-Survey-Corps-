# Executive Summary

The investigation reviewed 162 supported bank-statement files and reconstructed 183192 deduplicated transactions for 111 account identifiers.

The primary review priorities are:

1. Accounts with large credits followed by same-day or next-day ATM/cash withdrawals.
2. Accounts with high velocity UPI activity and repeated low-value or round-value transfers.
3. Repeated beneficiaries and UPI IDs appearing across multiple accounts.
4. Accounts with balances repeatedly rising sharply and then falling near zero.

The ground truth is conservative. Single-sided movements are marked medium confidence unless the opposite side was found in another statement.
