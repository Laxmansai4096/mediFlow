"""Request-scoped context (correlation id, service name, client ip)."""
from contextvars import ContextVar

correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")
service_name: ContextVar[str] = ContextVar("service_name", default="unknown")
client_ip: ContextVar[str] = ContextVar("client_ip", default="")
