"""Local DB and extraction-server host resolution.

DBs run on separate machines, the extraction server on another.  Each is
reachable over CSL first, then SACNet.  The chosen host is remembered per
process so every connection site in the app uses one stable answer.
"""
from __future__ import annotations

import socket

MONGO_HOSTS: tuple[str, str] = ("192.168.5.211", "10.61.82.211")
QDRANT_HOSTS: tuple[str, str] = ("192.168.5.212", "10.61.82.212")
NEO4J_HOSTS: tuple[str, str] = ("192.168.5.30", "10.61.82.30")
EXTRACT_HOSTS: tuple[str, str] = ("192.168.5.32", "10.61.82.32")

MONGO_PORT = 27017
QDRANT_PORT = 6333
NEO4J_PORT = 7687
EXTRACT_PORT = 8004

DEFAULT_TIMEOUT = 1.0

_memo: dict[str, str] = {}


def _reachable(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def resolve_host(hosts: tuple[str, str], port: int, label: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    if label in _memo:
        return _memo[label]
    for host in hosts:
        if _reachable(host, port, timeout):
            _memo[label] = host
            return host
    # ponytail: neither net answered; pick CSL and let the DB client raise the real error
    _memo[label] = hosts[0]
    return hosts[0]


def mongo_uri() -> str:
    return f"mongodb://{resolve_host(MONGO_HOSTS, MONGO_PORT, 'mongo')}:{MONGO_PORT}"


def qdrant_url() -> str:
    return f"http://{resolve_host(QDRANT_HOSTS, QDRANT_PORT, 'qdrant')}:{QDRANT_PORT}"


def neo4j_uri() -> str:
    return f"bolt://{resolve_host(NEO4J_HOSTS, NEO4J_PORT, 'neo4j')}:{NEO4J_PORT}"


def extraction_url() -> str:
    return f"http://{resolve_host(EXTRACT_HOSTS, EXTRACT_PORT, 'extract')}:{EXTRACT_PORT}"