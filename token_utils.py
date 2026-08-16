"""
Auth-MCP Token Utilities
========================

Copy this file to any project that needs to generate or validate auth-mcp tokens.

No dependencies beyond Python standard library.
No secrets or environment variables required.

Token Format:
    {random_16_hex}-{checksum_8_hex}
    Example: 3f312f72070f9ccb-11013be5

The checksum is SHA256(random_part)[:8] - purely mathematical, no secrets.
"""

import hashlib
import secrets


def generate_token() -> str:
    """
    Generate a valid auth-mcp token.

    Returns:
        Token string in format: {random}-{checksum}

    Example:
        >>> token = generate_token()
        >>> print(token)
        '3f312f72070f9ccb-11013be5'
    """
    random_part = secrets.token_hex(8)  # 16 hex chars
    checksum = hashlib.sha256(random_part.encode()).hexdigest()[:8]
    return f"{random_part}-{checksum}"


def validate_token(token: str) -> bool:
    """
    Validate an auth-mcp token by recalculating its checksum.

    Args:
        token: Token to validate

    Returns:
        True if valid, False otherwise

    Example:
        >>> validate_token('3f312f72070f9ccb-11013be5')
        True
        >>> validate_token('invalid-token')
        False
    """
    if not token or '-' not in token:
        return False

    parts = token.rsplit('-', 1)
    if len(parts) != 2:
        return False

    random_part, provided_checksum = parts

    # Validate format: 16 hex chars for random, 8 for checksum
    if len(random_part) != 16 or len(provided_checksum) != 8:
        return False

    # Verify hex characters
    try:
        int(random_part, 16)
        int(provided_checksum, 16)
    except ValueError:
        return False

    # Recalculate and compare
    expected = hashlib.sha256(random_part.encode()).hexdigest()[:8]
    return provided_checksum.lower() == expected.lower()


def get_or_create_token(env_var: str = 'AUTH_MCP_TOKEN') -> str:
    """
    Get token from environment or generate a new one.

    Args:
        env_var: Environment variable name to check

    Returns:
        Valid token (from env or newly generated)
    """
    import os
    token = os.environ.get(env_var)
    if token and validate_token(token):
        return token
    return generate_token()


# --- One-liners for quick use ---

# Generate: python -c "import secrets,hashlib;r=secrets.token_hex(8);print(f'{r}-{hashlib.sha256(r.encode()).hexdigest()[:8]}')"

# Validate: python -c "import hashlib;t='YOUR-TOKEN';r,c=t.rsplit('-',1);print('Valid' if hashlib.sha256(r.encode()).hexdigest()[:8]==c else 'Invalid')"


if __name__ == '__main__':
    # Demo
    print("Auth-MCP Token Utilities")
    print("=" * 40)

    # Generate
    token = generate_token()
    print(f"\nGenerated: {token}")

    # Validate
    is_valid = validate_token(token)
    print(f"Valid: {is_valid}")

    # Test invalid
    print(f"\nInvalid token test: {validate_token('bad-token')}")
    print(f"Wrong checksum test: {validate_token('0000000000000000-00000000')}")
