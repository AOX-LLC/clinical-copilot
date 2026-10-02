"""The production crypto objects a run needs, built from the key material.

``FieldSealer`` is the only sealer production code constructs. Everything here is built
from the two secrets in ``KeyMaterial``; nothing takes a sealer from a caller.
"""

from dataclasses import dataclass

from app.crypto.blind_index import BlindIndexer
from app.crypto.keyring import FieldSealer, KeyRing, KeyWrapper
from app.crypto.keys import KeyMaterial
from app.crypto.keystore import KeyStore


@dataclass(frozen=True, slots=True)
class FieldCrypto:
    keystore: KeyStore
    sealer: FieldSealer
    indexer: BlindIndexer


def build_field_crypto(material: KeyMaterial) -> FieldCrypto:
    wrapper = KeyWrapper(material.kek, material.kek_version)
    ring = KeyRing()
    return FieldCrypto(
        keystore=KeyStore(wrapper, ring),
        sealer=FieldSealer(ring),
        indexer=BlindIndexer(material.blind_index_key),
    )
