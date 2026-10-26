"""Vendored, dependency-free Colmi R02 packet codecs.

Copied from the upstream `colmi_r02_client` project (MIT-licensed). Only the pure parsing/packet-building modules are included; the upstream package's `Client`/`cli`/`db` (which pin `bleak<0.23` and pull in sqlalchemy/asyncclick) are intentionally omitted because this integration owns its own BLE transport.
"""
