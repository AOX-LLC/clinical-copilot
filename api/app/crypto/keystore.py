"""Loading, creating and destroying data keys in the database.

``KeyStore.load`` makes each owner's key available in the ``KeyRing``, creating and storing
it (wrapped) the first time. Two runs creating the same key race safely: the loser adopts
the winner's row. Destroying a key is an owner-role operation: the application role has no
UPDATE on ``data_key``, so application code cannot do it, by design.
"""

import uuid
from collections.abc import Iterable

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.crypto.errors import KeyUnavailableError
from app.crypto.keyring import KeyRing, KeyWrapper, generate_data_key
from app.timeline.models import DataKey


class KeyStore:
    def __init__(self, wrapper: KeyWrapper, ring: KeyRing) -> None:
        self._wrapper = wrapper
        self._ring = ring

    async def load(self, session: AsyncSession, owners: Iterable[uuid.UUID | None]) -> None:
        """Ensure a data key is in the ring for each owner (``None`` is the system key)."""
        for owner in dict.fromkeys(owners):
            if owner not in self._ring:
                await self._load_one(session, owner)

    async def _load_one(self, session: AsyncSession, owner: uuid.UUID | None) -> None:
        row = await _find(session, owner)
        if row is None:
            data_key = generate_data_key()
            created = await session.scalar(
                insert(DataKey)
                .values(
                    patient_id=owner,
                    kek_version=self._wrapper.kek_version,
                    wrapped_key=self._wrapper.wrap(data_key, owner),
                )
                .on_conflict_do_nothing()
                .returning(DataKey.id)
            )
            if created is not None:
                self._ring.add(owner, data_key)
                return
            row = await _find(session, owner)  # another run created it first: adopt that key
        if row is None or row.wrapped_key is None:
            raise KeyUnavailableError(f"the data key for {_label(owner)} was destroyed")
        if row.kek_version != self._wrapper.kek_version:
            raise KeyUnavailableError(
                f"the data key for {_label(owner)} is wrapped under key-encryption key "
                f"version {row.kek_version}, and this run holds version {self._wrapper.kek_version}"
            )
        self._ring.add(owner, self._wrapper.unwrap(row.wrapped_key, owner))


async def destroy_patient_key(session: AsyncSession, patient_id: uuid.UUID) -> bool:
    """Make a patient's encrypted fields unreadable for good. Owner role only.

    Returns False when the patient had no live key. Backups of the wrapped key would defeat
    this, so the database's backups must be retired on the same schedule as the data.
    """
    result = await session.execute(
        update(DataKey)
        .where(DataKey.patient_id == patient_id, DataKey.wrapped_key.is_not(None))
        .values(wrapped_key=None, destroyed_at=func.now())
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def _find(session: AsyncSession, owner: uuid.UUID | None) -> DataKey | None:
    condition = DataKey.patient_id.is_(None) if owner is None else DataKey.patient_id == owner
    return (await session.execute(select(DataKey).where(condition))).scalar_one_or_none()


def _label(owner: uuid.UUID | None) -> str:
    return "the system" if owner is None else f"patient {owner}"
