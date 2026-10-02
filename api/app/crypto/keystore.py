"""Loading, creating and destroying data keys in the database.

``KeyStore.load`` makes each owner's key available in the ``KeyRing``, creating and storing
it (wrapped) the first time. Two runs creating the same key race safely: the loser adopts
the winner's row. Destroying a key is an owner-role operation: the application role has no
UPDATE on ``data_key``, so application code cannot do it, by design.
"""

import uuid
from collections.abc import Callable, Iterable

from sqlalchemy import delete, event, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.crypto.errors import KeyUnavailableError
from app.crypto.keyring import KeyRing, KeyWrapper, generate_data_key
from app.timeline.models import DataKey, PatientBlindIndex


class KeyStore:
    """Keeps the ring in step with the ``data_key`` table.

    Every ``load`` re-reads the rows for the owners asked about, in one query, so a key
    destroyed by another process stops working at the next ``load`` instead of living on in a
    long-running one. Callers load keys at the start of each transaction.
    """

    def __init__(self, wrapper: KeyWrapper, ring: KeyRing) -> None:
        self._wrapper = wrapper
        self._ring = ring
        self._pending_key = ("pending_data_keys", id(self))

    async def load(self, session: AsyncSession, owners: Iterable[uuid.UUID | None]) -> None:
        """Ensure a live data key is in the ring for each owner (``None`` is the system key)."""
        wanted = list(dict.fromkeys(owners))
        rows = await _find_all(session, wanted)
        for owner in wanted:
            row = rows.get(owner)
            if row is None:
                await self._create(session, owner)
            else:
                self._adopt(owner, row)

    async def _create(self, session: AsyncSession, owner: uuid.UUID | None) -> None:
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
        if created is None:  # another run created it first: adopt that key
            row = (await _find_all(session, [owner])).get(owner)
            if row is None:
                raise KeyUnavailableError(f"the data key for {_label(owner)} cannot be created")
            self._adopt(owner, row)
            return
        self._ring.add(owner, data_key)
        self._forget_if_this_transaction_rolls_back(session, owner)

    def _adopt(self, owner: uuid.UUID | None, row: DataKey) -> None:
        if row.wrapped_key is None:
            self._ring.discard(owner)
            raise KeyUnavailableError(f"the data key for {_label(owner)} was destroyed")
        if row.kek_version != self._wrapper.kek_version:
            raise KeyUnavailableError(
                f"the data key for {_label(owner)} is wrapped under key-encryption key "
                f"version {row.kek_version}, and this run holds version {self._wrapper.kek_version}"
            )
        if owner not in self._ring:
            self._ring.add(owner, self._wrapper.unwrap(row.wrapped_key, owner))

    def _forget_if_this_transaction_rolls_back(
        self, session: AsyncSession, owner: uuid.UUID | None
    ) -> None:
        """A key created in a transaction that rolls back has no row, so it must not be reused.

        Sealing under it would produce ciphertext no stored key can open. Eviction is
        deliberately generous (any rollback drops the keys created since the last commit): a
        key whose row still exists is simply read back on the next ``load``.
        """
        pending: set[uuid.UUID | None] | None = session.info.get(self._pending_key)
        if pending is None:
            pending = session.info[self._pending_key] = set()
            event.listen(session.sync_session, "after_commit", lambda _session: pending.clear())
            event.listen(session.sync_session, "after_soft_rollback", self._evict(pending))
        pending.add(owner)

    def _evict(self, pending: set[uuid.UUID | None]) -> Callable[[object, object], None]:
        def evict(_session: object, _previous_transaction: object) -> None:
            for owner in pending:
                self._ring.discard(owner)
            pending.clear()

        return evict


async def destroy_patient_key(session: AsyncSession, patient_id: uuid.UUID) -> bool:
    """Make a patient's encrypted fields unreadable for good, and remove them from lookup.

    The patient's blind-index rows go in the same transaction: they are digests of the name,
    birth date and identifiers, and whoever holds the blind-index key could test guesses
    against them. Owner role only. Returns False when the patient had no live key.

    This does not remove the plaintext timeline columns (codes, values, times) or the
    source link, which are plaintext by design (ADR 0008). Backups holding the wrapped key
    would also defeat it, so backups must be retired on the same schedule as the data.
    """
    await session.execute(
        delete(PatientBlindIndex).where(PatientBlindIndex.patient_id == patient_id)
    )
    result = await session.execute(
        update(DataKey)
        .where(DataKey.patient_id == patient_id, DataKey.wrapped_key.is_not(None))
        .values(wrapped_key=None, destroyed_at=func.now())
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def _find_all(
    session: AsyncSession, owners: list[uuid.UUID | None]
) -> dict[uuid.UUID | None, DataKey]:
    """The key rows for these owners in one query, by owner."""
    patients = [owner for owner in owners if owner is not None]
    conditions = [DataKey.patient_id.in_(patients)] if patients else []
    if None in owners:
        conditions.append(DataKey.patient_id.is_(None))
    if not conditions:
        return {}
    rows = (await session.execute(select(DataKey).where(or_(*conditions)))).scalars()
    return {row.patient_id: row for row in rows}


def _label(owner: uuid.UUID | None) -> str:
    return "the system" if owner is None else f"patient {owner}"
