"""Fixed-roster alpha approval log with durable locks and verified replay.

This supplies certificate safety, not permissionless admission or round-changing
BFT liveness. Conflicting proposals stay locked; resume the original proposal.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from rynmesh.crypto import canonical_json, sha256_bytes, sign_payload

from .protocol import (
    CLOCK_SKEW,
    ORDER_ACTIONS,
    TERMINAL,
    VERSION,
    case_payload,
    genesis,
    initial_state,
    quorum,
    require,
    signature,
    vote_deadlines,
)

MAX_DATABASE_BYTES = 128 * 1024 * 1024
ORDER_STORAGE_RESERVE = 16 * 1024 * 1024
ADMISSION_HEADROOM = 16 * 1024 * 1024


class Ledger:
    def __init__(self, home: Path, private_key: bytes, *, clock=time.time):
        self.path = home / "exchange" / "ledger.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        self.private_key = private_key
        self.clock = clock
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        os.chmod(self.path, 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS blocks (height INTEGER PRIMARY KEY, hash TEXT UNIQUE, certificate TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS votes (height INTEGER PRIMARY KEY, hash TEXT, proposal TEXT, vote TEXT);
            CREATE TABLE IF NOT EXISTS proposals (height INTEGER PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS intents (id TEXT PRIMARY KEY, fingerprint TEXT, command TEXT NOT NULL, status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS work (id TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS work_clock (id TEXT PRIMARY KEY, created INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY, count INTEGER NOT NULL, created INTEGER NOT NULL);
        """)
        self.db.commit()
        self.state: dict | None = None
        self.config: dict | None = None
        self.network = ""
        self.height = 0
        self.head = ""
        config = self.setting("manifest")
        if config:
            self.config = genesis(config)
            self.network = sha256_bytes(canonical_json(self.config))
            self.state = self.rules().initial_state(self.config)
            self.head = self.network
            for stored_height, stored_hash, raw in self.db.execute(
                "SELECT height, hash, certificate FROM blocks ORDER BY height"
            ):
                certificate = json.loads(raw)
                replayed = self._verify(certificate)
                proposal = certificate["proposal"]
                require(
                    stored_height == self.height + 1
                    and stored_hash == sha256_bytes(canonical_json(proposal)),
                    "exchange_log_corrupt",
                )
                self.state = replayed
                self.height = stored_height
                self.head = stored_hash

    def setting(self, name: str) -> Any:
        with self.lock:
            row = self.db.execute("SELECT value FROM settings WHERE key=?", (name,)).fetchone()
            return json.loads(row[0]) if row else None

    def set_setting(self, name: str, value: Any) -> None:
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO settings VALUES (?,?)", (name, json.dumps(value))
            )

    def rules(self):
        from . import legacy_v1, protocol

        return protocol if self.config["version"] == VERSION else legacy_v1

    def writable(self):
        self.ready()
        require(self.config["version"] == VERSION, "exchange_legacy_read_only")

    def configure(self, manifest: dict) -> None:
        config = genesis(manifest)
        require(config["version"] == VERSION, "exchange_legacy_read_only")
        network = sha256_bytes(canonical_json(config))
        with self.lock:
            require(not self.network or self.network == network, "exchange_network_changed")
            self.set_setting("manifest", config)
            self.config = config
            self.network = network
            if self.state is None:
                self.state = initial_state(config)
                self.head = network

    def ready(self) -> None:
        require(self.state is not None and self.config is not None, "exchange_network_required")

    def _proposal(self, proposal: dict) -> dict:
        self.ready()
        require(
            isinstance(proposal, dict)
            and set(proposal)
            == {"version", "network", "height", "parent", "timestamp", "command", "proposer"}
        )
        require(
            proposal["version"] == self.config["version"] and proposal["network"] == self.network
        )
        require(
            type(proposal["height"]) is int and proposal["height"] == self.height + 1,
            "exchange_height_changed",
        )
        require(proposal["parent"] == self.head, "exchange_fork")
        proposer = signature(proposal["proposer"])
        core = {k: v for k, v in proposal.items() if k != "proposer"}
        leader = self.config["validators"][
            (proposal["height"] - 1) % len(self.config["validators"])
        ]["peer_id"]
        require(
            proposer.public_key == leader and proposer.payload == core, "exchange_proposer_invalid"
        )
        require(
            type(proposal["timestamp"]) is int and proposal["timestamp"] >= 0,
            "exchange_time_invalid",
        )
        return self.rules().apply(
            self.state, proposal["command"], self.config, proposal["timestamp"]
        )

    def vote(self, proposal: dict) -> dict:
        from rynmesh.crypto import public_key_from_private

        with self.lock, self.db:
            self.writable()
            own_key = public_key_from_private(self.private_key)
            require(
                own_key in {v["peer_id"] for v in self.config["validators"]},
                "exchange_not_validator",
            )
            self._proposal(proposal)
            digest = sha256_bytes(canonical_json(proposal))
            prior = self.db.execute(
                "SELECT hash, vote FROM votes WHERE height=?", (proposal["height"],)
            ).fetchone()
            if prior:
                require(prior[0] == digest, "exchange_height_locked")
                return json.loads(prior[1])
            now = int(self.clock())
            require(abs(proposal["timestamp"] - now) <= CLOCK_SKEW, "exchange_time_invalid")
            vote_deadlines(self.state, proposal["command"]["payload"], now)
            self.admit_storage(proposal["command"]["payload"]["action"])

            vote = sign_payload(
                {
                    "version": VERSION,
                    "kind": "approval",
                    "network": self.network,
                    "height": proposal["height"],
                    "hash": digest,
                },
                private_key_bytes=self.private_key,
            ).to_dict()
            self.db.execute(
                "INSERT INTO votes VALUES (?,?,?,?)",
                (proposal["height"], digest, json.dumps(proposal), json.dumps(vote)),
            )
            return vote

    def _verify(self, certificate: dict) -> dict:
        require(isinstance(certificate, dict) and set(certificate) == {"proposal", "votes"})
        proposal = certificate["proposal"]
        next_state = self._proposal(proposal)
        digest = sha256_bytes(canonical_json(proposal))
        expected = {
            "version": self.config["version"],
            "kind": "approval",
            "network": self.network,
            "height": proposal["height"],
            "hash": digest,
        }
        votes = certificate["votes"]
        require(isinstance(votes, list) and len(votes) <= 8)
        signers = set()
        for wire in votes:
            vote = signature(wire)
            require(
                vote.public_key in {v["peer_id"] for v in self.config["validators"]}
                and vote.payload == expected
            )
            require(vote.public_key not in signers, "exchange_duplicate_vote")
            signers.add(vote.public_key)
        require(len(signers) >= quorum(self.config), "exchange_quorum_unavailable")
        return next_state

    def commit(self, certificate: dict) -> dict:
        with self.lock:
            self.ready()
            require(isinstance(certificate, dict) and isinstance(certificate.get("proposal"), dict))
            proposal = certificate["proposal"]
            digest = sha256_bytes(canonical_json(proposal))
            prior = self.db.execute(
                "SELECT hash FROM blocks WHERE height=?", (proposal.get("height"),)
            ).fetchone()
            if prior:
                require(prior[0] == digest, "exchange_fork")
                return {"height": proposal["height"], "hash": digest, "committed": True}
            new_state = self._verify(certificate)
            with self.db:
                self.db.execute(
                    "INSERT INTO blocks VALUES (?,?,?)",
                    (proposal["height"], digest, json.dumps(certificate)),
                )
                self.db.execute(
                    "UPDATE intents SET status='committed' WHERE id=?",
                    (proposal["command"]["payload"]["id"],),
                )
            self.state = new_state
            self.height = proposal["height"]
            self.head = digest
            if self.config["version"] == VERSION:
                self.prune_work()
                try:
                    self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.OperationalError:
                    pass
            return {"height": self.height, "hash": digest, "committed": True}

    def admit_storage(self, action):
        if action in ORDER_ACTIONS:
            return  # This order reserved its bounded lifecycle before escrow.
        opened = sum(o["status"] not in TERMINAL for o in self.state["orders"].values())
        if action == "agree":
            opened += 1
        size = sum(p.stat().st_size for p in self.path.parent.glob("ledger.sqlite3*"))
        require(
            size + opened * ORDER_STORAGE_RESERVE + ADMISSION_HEADROOM < MAX_DATABASE_BYTES,
            "exchange_admission_exhausted",
        )

    def prune_work(self):
        """Only disposable receipts/attempts; never history, intents or vote locks."""
        with self.lock, self.db:
            active = {
                "case:" + sha256_bytes(canonical_json(case_payload(self.config, o, o["round"])))
                for o in self.state["orders"].values()
                if o["status"] == "disputed" and len(o["evidence"]) == 2
            }
            for (item,) in self.db.execute("SELECT id FROM work WHERE id LIKE 'case:%'").fetchall():
                if item not in active:
                    self.db.execute("DELETE FROM work WHERE id=?", (item,))
            for (item,) in self.db.execute(
                "SELECT id FROM attempts WHERE id LIKE 'case:%'"
            ).fetchall():
                if item not in active:
                    self.db.execute("DELETE FROM attempts WHERE id=?", (item,))
            self.db.execute(
                "DELETE FROM work WHERE id IN (SELECT id FROM work_clock WHERE created<?)",
                (int(self.clock()) - 600,),
            )
            self.db.execute("DELETE FROM work_clock WHERE id NOT IN (SELECT id FROM work)")
            self.db.execute(
                "DELETE FROM attempts WHERE id LIKE 'work:%' AND created<?",
                (int(self.clock()) - 600,),
            )
            for provider in self.state["rewards"].values():
                self.db.execute("DELETE FROM work WHERE id=?", ("work:" + provider,))
                self.db.execute("DELETE FROM work_clock WHERE id=?", ("work:" + provider,))
                self.db.execute("DELETE FROM attempts WHERE id=?", ("work:" + provider,))

    def attempt(self, item):
        with self.lock, self.db:
            row = self.db.execute("SELECT count FROM attempts WHERE id=?", (item,)).fetchone()
            require(not row or row[0] < 3, "exchange_attempt_limit")
            require(
                row
                or self.db.execute(
                    "SELECT count(*) FROM attempts WHERE id LIKE ?", (item.split(":")[0] + ":%",)
                ).fetchone()[0]
                < 1000,
                "exchange_capacity",
            )
            self.db.execute(
                "INSERT INTO attempts VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET count=count+1",
                (item, 1, int(self.clock())),
            )

    def cache_work(self, item, value):
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO work VALUES (?,?)", (item, json.dumps(value)))
            self.db.execute(
                "INSERT OR IGNORE INTO work_clock VALUES (?,?)", (item, int(self.clock()))
            )

    def blocks(self, after: int = 0) -> dict:
        with self.lock:
            self.ready()
            require(type(after) is int and after >= 0)
            entries = []
            size = 0
            for row in self.db.execute(
                "SELECT certificate FROM blocks WHERE height>? ORDER BY height LIMIT 10", (after,)
            ):
                if entries and size + len(row[0].encode()) > 2 * 1024 * 1024:
                    break
                entries.append(json.loads(row[0]))
                size += len(row[0].encode())
            return {
                "network": self.network,
                "height": self.height,
                "head": self.head,
                "blocks": entries,
            }

    def pending(self) -> list[dict]:
        with self.lock:
            votes = [
                json.loads(row[0])
                for row in self.db.execute(
                    "SELECT proposal FROM votes WHERE height>? ORDER BY height LIMIT 1",
                    (self.height,),
                )
            ]
            return votes or [
                json.loads(row[0])
                for row in self.db.execute(
                    "SELECT value FROM proposals WHERE height>? ORDER BY height LIMIT 1",
                    (self.height,),
                )
            ]

    def intents(self) -> list[dict]:
        with self.lock:
            return [
                {"id": row[0], "status": row[1], "action": json.loads(row[2])["payload"]["action"]}
                for row in self.db.execute(
                    "SELECT id,status,command FROM intents WHERE status='pending' LIMIT 20"
                )
            ]

    def receipts(self):
        with self.lock:
            return [
                {"id": row[0], "status": row[1], "action": json.loads(row[2])["payload"]["action"]}
                for row in self.db.execute(
                    "SELECT id,status,command FROM intents ORDER BY rowid DESC LIMIT 20"
                )
            ]

    def close(self) -> None:
        self.db.close()
