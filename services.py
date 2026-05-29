from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
from typing import Any


def _utc_now_iso() -> str:
	return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class WorkflowServiceRecord:
	service_id: str
	node_class: str
	status: str = "registered"
	is_running: bool = False
	pid: int | None = None
	installed: bool = False
	last_error: str = ""
	metadata: dict[str, Any] = field(default_factory=dict)
	created_at: str = field(default_factory=_utc_now_iso)
	updated_at: str = field(default_factory=_utc_now_iso)


class WorkflowServiceRegistryCenter:
	def __init__(self) -> None:
		self._records: dict[str, WorkflowServiceRecord] = {}
		self._lock = RLock()

	def register_service(
		self,
		service_id: str,
		*,
		node_class: str,
		metadata: dict[str, Any] | None = None,
	) -> WorkflowServiceRecord:
		sid = str(service_id).strip()
		if not sid:
			raise ValueError("service_id must not be empty")

		with self._lock:
			existing = self._records.get(sid)
			if existing is not None:
				existing.node_class = node_class
				if metadata:
					existing.metadata.update(metadata)
				existing.updated_at = _utc_now_iso()
				return existing

			record = WorkflowServiceRecord(
				service_id=sid,
				node_class=str(node_class).strip() or "WorkflowServiceNode",
				metadata=dict(metadata or {}),
			)
			self._records[sid] = record
			return record

	def update_service_status(
		self,
		service_id: str,
		*,
		status: str,
		is_running: bool | None = None,
		pid: int | None = None,
		installed: bool | None = None,
		last_error: str | None = None,
		metadata: dict[str, Any] | None = None,
	) -> WorkflowServiceRecord:
		sid = str(service_id).strip()
		if not sid:
			raise ValueError("service_id must not be empty")

		with self._lock:
			record = self._records.get(sid)
			if record is None:
				record = WorkflowServiceRecord(service_id=sid, node_class="WorkflowServiceNode")
				self._records[sid] = record

			record.status = str(status).strip() or record.status
			if is_running is not None:
				record.is_running = bool(is_running)
			if pid is not None:
				record.pid = int(pid)
			if installed is not None:
				record.installed = bool(installed)
			if last_error is not None:
				record.last_error = str(last_error)
			if metadata:
				record.metadata.update(metadata)

			record.updated_at = _utc_now_iso()
			return record

	def mark_failed(self, service_id: str, error: str) -> WorkflowServiceRecord:
		return self.update_service_status(
			service_id,
			status="failed",
			is_running=False,
			last_error=error,
		)

	def get_service(self, service_id: str) -> WorkflowServiceRecord | None:
		sid = str(service_id).strip()
		if not sid:
			return None
		with self._lock:
			return self._records.get(sid)

	def is_service_running(self, service_id: str) -> bool:
		record = self.get_service(service_id)
		return bool(record is not None and record.is_running)

	def require_running(self, service_id: str) -> WorkflowServiceRecord:
		record = self.get_service(service_id)
		if record is None:
			raise RuntimeError(f"service '{service_id}' is not registered")
		if not record.is_running:
			raise RuntimeError(f"service '{service_id}' is not running")
		return record

	def list_services(self) -> list[WorkflowServiceRecord]:
		with self._lock:
			return list(self._records.values())

	def remove_service(self, service_id: str) -> bool:
		sid = str(service_id).strip()
		if not sid:
			return False
		with self._lock:
			return self._records.pop(sid, None) is not None

	def clear(self) -> None:
		with self._lock:
			self._records.clear()


workflow_service_registry = WorkflowServiceRegistryCenter()

