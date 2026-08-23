from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from app.core.errors import StorageUnavailableError
from app.core.models import Lease, ReservationRequest, ResourceClaim, ResourceVector
from app.persistence.models import (
    LeaseTerminalReason,
    ReservationTerminalReason,
    StoredAcquireRequest,
    StoredReservation,
)

SCHEMA_VERSION = 1
APPLICATION_ID = 0x4A525452  # ASCII-like marker for "JRTR".
UTC = timezone.utc


class SQLiteStateRepository:
    """SQLite-backed authoritative state for one Router process."""

    def __init__(self, database_path: str | Path = ":memory:") -> None:
        raw_database_path = str(database_path)
        self._database_path = (
            raw_database_path
            if raw_database_path == ":memory:"
            else str(Path(raw_database_path).expanduser().resolve())
        )
        self._lock = threading.RLock()
        try:
            if self._database_path != ":memory:":
                Path(self._database_path).parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )
            self._connection = sqlite3.connect(
                self._database_path,
                timeout=5.0,
                isolation_level=None,
                check_same_thread=False,
            )
            self._connection.row_factory = sqlite3.Row
            self._configure()
            self._migrate()
            self._verify_integrity()
        except StorageUnavailableError:
            self._close_after_failed_initialization()
            raise
        except (OSError, sqlite3.Error) as error:
            self._close_after_failed_initialization()
            raise StorageUnavailableError(
                f"could not initialize SQLite state at {self._database_path}"
            ) from error

    @property
    def database_path(self) -> str:
        return self._database_path

    def load_active_leases(self) -> tuple[Lease, ...]:
        rows = self._fetchall(
            """
            SELECT lease_id, request_id, client_id, mode, resources_json,
                   granted_at, expires_at
            FROM leases
            WHERE lifecycle = 'ACTIVE'
            ORDER BY granted_at, lease_id
            """
        )
        return tuple(self._decode_lease(row) for row in rows)

    def get_acquire_request(
        self,
        request_id: UUID,
    ) -> StoredAcquireRequest | None:
        row = self._fetchone(
            """
            SELECT request.request_json, request.lease_id, lease.lifecycle
            FROM acquire_requests AS request
            JOIN leases AS lease ON lease.lease_id = request.lease_id
            WHERE request.request_id = ?
            """,
            (str(request_id),),
        )
        if row is None:
            return None
        try:
            lifecycle = str(row["lifecycle"])
            lease_end = (
                None
                if lifecycle == "ACTIVE"
                else LeaseTerminalReason(lifecycle)
            )
            return StoredAcquireRequest(
                claim=ResourceClaim.model_validate_json(row["request_json"]),
                lease_id=UUID(row["lease_id"]),
                lease_end=lease_end,
            )
        except (TypeError, ValueError) as error:
            raise StorageUnavailableError(
                "persisted acquire request is invalid"
            ) from error

    def get_lease_lifecycle(self, lease_id: UUID) -> str | None:
        row = self._fetchone(
            "SELECT lifecycle FROM leases WHERE lease_id = ?",
            (str(lease_id),),
        )
        return None if row is None else str(row["lifecycle"])

    def record_grant(self, claim: ResourceClaim, lease: Lease) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO leases (
                    lease_id, request_id, client_id, mode, resources_json,
                    granted_at, expires_at, lifecycle, terminal_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', NULL)
                """,
                (
                    str(lease.lease_id),
                    str(lease.request_id),
                    lease.client_id,
                    lease.mode.value,
                    _model_json(lease.resources),
                    _timestamp(lease.granted_at),
                    _timestamp(lease.expires_at),
                ),
            )
            connection.execute(
                """
                INSERT INTO acquire_requests (
                    request_id, request_json, lease_id, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    str(claim.request_id),
                    _model_json(claim),
                    str(lease.lease_id),
                    _timestamp(lease.granted_at),
                ),
            )

    def renew_lease(self, lease: Lease) -> None:
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE leases
                SET expires_at = ?
                WHERE lease_id = ? AND lifecycle = 'ACTIVE'
                """,
                (_timestamp(lease.expires_at), str(lease.lease_id)),
            )
            if cursor.rowcount != 1:
                raise StorageUnavailableError(
                    f"active lease {lease.lease_id} is missing from SQLite"
                )

    def terminalize_leases(
        self,
        updates: Iterable[
            tuple[UUID, LeaseTerminalReason, datetime]
        ],
    ) -> None:
        materialized = tuple(updates)
        if not materialized:
            return
        with self._transaction() as connection:
            for lease_id, reason, terminal_at in materialized:
                cursor = connection.execute(
                    """
                    UPDATE leases
                    SET lifecycle = ?, terminal_at = ?
                    WHERE lease_id = ? AND lifecycle = 'ACTIVE'
                    """,
                    (
                        reason.value,
                        _timestamp(terminal_at),
                        str(lease_id),
                    ),
                )
                if cursor.rowcount != 1:
                    raise StorageUnavailableError(
                        f"active lease {lease_id} is missing from SQLite"
                    )

    def load_nonterminal_reservations(
        self,
    ) -> tuple[StoredReservation, ...]:
        rows = self._fetchall(
            """
            SELECT request_json, drain_at, lease_deadline, end_at,
                   activated_at, terminal_reason, terminal_at
            FROM reservations
            WHERE terminal_reason IS NULL
            ORDER BY drain_at, reservation_id
            """
        )
        return tuple(self._decode_reservation(row) for row in rows)

    def get_reservation(
        self,
        reservation_id: str,
    ) -> StoredReservation | None:
        row = self._fetchone(
            """
            SELECT request_json, drain_at, lease_deadline, end_at,
                   activated_at, terminal_reason, terminal_at
            FROM reservations
            WHERE reservation_id = ?
            """,
            (reservation_id,),
        )
        return None if row is None else self._decode_reservation(row)

    def record_reservation(
        self,
        reservation: StoredReservation,
        *,
        created_at: datetime,
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO reservations (
                    reservation_id, request_json, drain_at, lease_deadline,
                    end_at, activated_at, terminal_reason, terminal_at,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, ?)
                """,
                (
                    reservation.request.reservation_id,
                    _model_json(reservation.request),
                    _timestamp(reservation.drain_at),
                    _timestamp(reservation.lease_deadline),
                    _timestamp(reservation.end_at),
                    _timestamp(created_at),
                ),
            )

    def terminalize_and_activate_reservation(
        self,
        *,
        ended_reservations: Iterable[tuple[str, datetime]],
        activated_reservation: tuple[str, datetime] | None,
    ) -> None:
        ended = tuple(ended_reservations)
        if not ended and activated_reservation is None:
            return
        with self._transaction() as connection:
            for reservation_id, terminal_at in ended:
                cursor = connection.execute(
                    """
                    UPDATE reservations
                    SET terminal_reason = 'ENDED', terminal_at = ?
                    WHERE reservation_id = ? AND terminal_reason IS NULL
                    """,
                    (_timestamp(terminal_at), reservation_id),
                )
                if cursor.rowcount != 1:
                    raise StorageUnavailableError(
                        f"nonterminal reservation {reservation_id} "
                        "is missing from SQLite"
                    )
            if activated_reservation is not None:
                reservation_id, activated_at = activated_reservation
                cursor = connection.execute(
                    """
                    UPDATE reservations
                    SET activated_at = ?
                    WHERE reservation_id = ?
                      AND activated_at IS NULL
                      AND terminal_reason IS NULL
                    """,
                    (_timestamp(activated_at), reservation_id),
                )
                if cursor.rowcount != 1:
                    raise StorageUnavailableError(
                        f"nonterminal reservation {reservation_id} "
                        "is missing from SQLite"
                    )

    def terminalize_reservation(
        self,
        reservation_id: str,
        reason: ReservationTerminalReason,
        terminal_at: datetime,
    ) -> None:
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE reservations
                SET terminal_reason = ?, terminal_at = ?
                WHERE reservation_id = ? AND terminal_reason IS NULL
                """,
                (
                    reason.value,
                    _timestamp(terminal_at),
                    reservation_id,
                ),
            )
            if cursor.rowcount != 1:
                raise StorageUnavailableError(
                    f"nonterminal reservation {reservation_id} "
                    "is missing from SQLite"
                )

    def check_health(self) -> None:
        row = self._fetchone("SELECT 1")
        if row is None or int(row[0]) != 1:
            raise StorageUnavailableError("SQLite health query failed")

    def close(self) -> None:
        with self._lock:
            try:
                self._connection.close()
            except sqlite3.Error as error:
                raise StorageUnavailableError(
                    "could not close SQLite state cleanly"
                ) from error

    def _configure(self) -> None:
        with self._lock:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 5000")
            journal_mode = str(
                self._connection.execute(
                    "PRAGMA journal_mode = WAL"
                ).fetchone()[0]
            ).lower()
            if self._database_path != ":memory:" and journal_mode != "wal":
                raise sqlite3.OperationalError(
                    f"SQLite did not enter WAL mode: {journal_mode}"
                )
            self._connection.execute("PRAGMA synchronous = FULL")
            self._connection.execute("PRAGMA wal_autocheckpoint = 1000")
            configured = {
                "foreign_keys": int(
                    self._connection.execute(
                        "PRAGMA foreign_keys"
                    ).fetchone()[0]
                ),
                "synchronous": int(
                    self._connection.execute(
                        "PRAGMA synchronous"
                    ).fetchone()[0]
                ),
                "busy_timeout": int(
                    self._connection.execute(
                        "PRAGMA busy_timeout"
                    ).fetchone()[0]
                ),
            }
            if configured != {
                "foreign_keys": 1,
                "synchronous": 2,
                "busy_timeout": 5000,
            }:
                raise sqlite3.OperationalError(
                    f"SQLite safety pragmas were not applied: {configured}"
                )

    def _close_after_failed_initialization(self) -> None:
        connection = getattr(self, "_connection", None)
        if connection is None:
            return
        try:
            connection.close()
        except sqlite3.Error:
            pass

    def _migrate(self) -> None:
        with self._lock:
            version = int(
                self._connection.execute("PRAGMA user_version").fetchone()[0]
            )
            application_id = int(
                self._connection.execute(
                    "PRAGMA application_id"
                ).fetchone()[0]
            )
            if version > SCHEMA_VERSION:
                raise sqlite3.DatabaseError(
                    f"database schema {version} is newer than supported "
                    f"version {SCHEMA_VERSION}"
                )
            if version > 0 and application_id != APPLICATION_ID:
                raise sqlite3.DatabaseError(
                    "SQLite file is not a Jetson Resource Router database"
                )
            if version == 0 and application_id not in (0, APPLICATION_ID):
                raise sqlite3.DatabaseError(
                    "SQLite file has an unrecognized application identifier"
                )
            if version == SCHEMA_VERSION:
                return
            existing_tables = tuple(
                row[0]
                for row in self._connection.execute(
                    """
                    SELECT name
                    FROM sqlite_schema
                    WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                    """
                ).fetchall()
            )
            if existing_tables:
                raise sqlite3.DatabaseError(
                    "refusing to initialize a non-empty unrecognized SQLite file"
                )

            try:
                self._connection.executescript(
                    f"""
                BEGIN IMMEDIATE;

                CREATE TABLE IF NOT EXISTS leases (
                    lease_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    client_id TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK (mode IN ('SHARED', 'EXCLUSIVE')),
                    resources_json TEXT NOT NULL,
                    granted_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    lifecycle TEXT NOT NULL
                        CHECK (lifecycle IN ('ACTIVE', 'EXPIRED', 'RELEASED')),
                    terminal_at TEXT,
                    CHECK (
                        (lifecycle = 'ACTIVE' AND terminal_at IS NULL)
                        OR
                        (lifecycle != 'ACTIVE' AND terminal_at IS NOT NULL)
                    )
                );

                CREATE TABLE IF NOT EXISTS acquire_requests (
                    request_id TEXT PRIMARY KEY,
                    request_json TEXT NOT NULL,
                    lease_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (lease_id) REFERENCES leases(lease_id)
                        ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS reservations (
                    reservation_id TEXT PRIMARY KEY,
                    request_json TEXT NOT NULL,
                    drain_at TEXT NOT NULL,
                    lease_deadline TEXT NOT NULL,
                    end_at TEXT NOT NULL,
                    activated_at TEXT,
                    terminal_reason TEXT
                        CHECK (
                            terminal_reason IS NULL
                            OR terminal_reason IN ('ENDED', 'CANCELLED')
                        ),
                    terminal_at TEXT,
                    created_at TEXT NOT NULL,
                    CHECK (
                        (terminal_reason IS NULL AND terminal_at IS NULL)
                        OR
                        (terminal_reason IS NOT NULL AND terminal_at IS NOT NULL)
                    )
                );

                CREATE INDEX IF NOT EXISTS leases_lifecycle_expiry
                    ON leases(lifecycle, expires_at);

                CREATE INDEX IF NOT EXISTS reservations_terminal_end
                    ON reservations(terminal_reason, end_at);

                PRAGMA application_id = {APPLICATION_ID};
                PRAGMA user_version = 1;
                COMMIT;
                """
                )
            except sqlite3.Error:
                try:
                    self._connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def _verify_integrity(self) -> None:
        row = self._fetchone("PRAGMA quick_check(1)")
        if row is None or str(row[0]).lower() != "ok":
            raise StorageUnavailableError("SQLite quick_check did not return ok")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                yield self._connection
                self._connection.execute("COMMIT")
            except sqlite3.Error as error:
                try:
                    self._connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise StorageUnavailableError(
                    "SQLite transaction failed"
                ) from error
            except Exception:
                try:
                    self._connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def _fetchone(
        self,
        statement: str,
        parameters: tuple[Any, ...] = (),
    ) -> sqlite3.Row | None:
        with self._lock:
            try:
                return self._connection.execute(
                    statement,
                    parameters,
                ).fetchone()
            except sqlite3.Error as error:
                raise StorageUnavailableError("SQLite read failed") from error

    def _fetchall(
        self,
        statement: str,
        parameters: tuple[Any, ...] = (),
    ) -> tuple[sqlite3.Row, ...]:
        with self._lock:
            try:
                return tuple(
                    self._connection.execute(
                        statement,
                        parameters,
                    ).fetchall()
                )
            except sqlite3.Error as error:
                raise StorageUnavailableError("SQLite read failed") from error

    @staticmethod
    def _decode_lease(row: sqlite3.Row) -> Lease:
        try:
            return Lease(
                lease_id=UUID(row["lease_id"]),
                request_id=UUID(row["request_id"]),
                client_id=row["client_id"],
                mode=row["mode"],
                resources=ResourceVector.model_validate_json(
                    row["resources_json"]
                ),
                granted_at=_parse_timestamp(row["granted_at"]),
                expires_at=_parse_timestamp(row["expires_at"]),
            )
        except (TypeError, ValueError) as error:
            raise StorageUnavailableError(
                "persisted active lease is invalid"
            ) from error

    @staticmethod
    def _decode_reservation(row: sqlite3.Row) -> StoredReservation:
        try:
            terminal_reason = row["terminal_reason"]
            return StoredReservation(
                request=ReservationRequest.model_validate_json(
                    row["request_json"]
                ),
                drain_at=_parse_timestamp(row["drain_at"]),
                lease_deadline=_parse_timestamp(row["lease_deadline"]),
                end_at=_parse_timestamp(row["end_at"]),
                activated_at=(
                    None
                    if row["activated_at"] is None
                    else _parse_timestamp(row["activated_at"])
                ),
                terminal_reason=(
                    None
                    if terminal_reason is None
                    else ReservationTerminalReason(terminal_reason)
                ),
                terminal_at=(
                    None
                    if row["terminal_at"] is None
                    else _parse_timestamp(row["terminal_at"])
                ),
            )
        except (TypeError, ValueError) as error:
            raise StorageUnavailableError(
                "persisted reservation is invalid"
            ) from error


def _model_json(model: Any) -> str:
    return json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("persisted timestamps must include a timezone")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("persisted timestamp has no timezone")
    return parsed
