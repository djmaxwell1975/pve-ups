"""Backward-compatible facade for host target operations.

Older integrations and tests import ``app.proxmox``. Keep that import stable while
the implementation now dispatches between Proxmox VE and PBS profiles.
"""

from .host_targets import TestResult, _auth_header, _client, shutdown_node, test_connection

__all__ = ["TestResult", "_auth_header", "_client", "shutdown_node", "test_connection"]
