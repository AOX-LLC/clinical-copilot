"""Crypto failures. Messages name the kind of failure, never plaintext, keys or ciphertext."""


class CryptoError(Exception):
    """Base for every failure in this package."""


class DecryptionError(CryptoError):
    """A ciphertext could not be opened: wrong key, wrong place, or altered."""


class KeyUnavailableError(CryptoError):
    """The data key for an owner is not loaded, was destroyed, or cannot be unwrapped."""


class KeyDestroyedError(KeyUnavailableError):
    """The patient's data key was destroyed on purpose; their sealed data is gone for good."""


class KeyMaterialError(CryptoError):
    """The key-encryption key or the blind-index key is missing or malformed."""
