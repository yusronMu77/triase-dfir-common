"""Bagian OpenTelemetry yang hanya dimuat bila extra `otel` terpasang (diimpor malas dari `tracing`).

- `AstriaIdGenerator`: span akar memakai trace id yang dipaksakan (`forced_trace_id`) bila ada, mis. trace id dari
  amplop event, sehingga konsumen dan relay outbox masuk ke trace yang sama dengan permintaan asalnya.
- `XTraceIdPropagator`: permintaan yang hanya membawa `X-Trace-Id` (tanpa `traceparent`) tetap memakai trace id
  itu; saat menyuntik, `X-Trace-Id` ikut dikirim agar penerima tanpa OpenTelemetry bisa mencatatnya.
"""

from __future__ import annotations

import contextvars
from collections.abc import Sequence

from opentelemetry import propagate, trace
from opentelemetry.context import Context
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.propagators.textmap import (
    CarrierT,
    Getter,
    Setter,
    TextMapPropagator,
    default_getter,
    default_setter,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.id_generator import RandomIdGenerator
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

FORCED_TRACE_ID: contextvars.ContextVar[int | None] = contextvars.ContextVar("astria_forced_trace_id", default=None)
X_TRACE_ID = "x-trace-id"


def parse_trace_id(value: str | None) -> int | None:
    """32 hex huruf kecil dan bukan nol semua; selain itu None."""
    if not value or len(value) != 32:
        return None
    try:
        number = int(value, 16)
    except ValueError:
        return None
    return number or None


class AstriaIdGenerator(RandomIdGenerator):
    def generate_trace_id(self) -> int:
        return FORCED_TRACE_ID.get() or super().generate_trace_id()

    def is_trace_id_random(self) -> bool:
        # Bendera W3C `random-trace-id` hanya untuk id buatan sendiri; id paksaan berasal dari amplop atau header.
        return FORCED_TRACE_ID.get() is None


class XTraceIdPropagator(TextMapPropagator):
    def extract(self, carrier: CarrierT, context: Context | None = None, getter: Getter = default_getter) -> Context:
        ctx = context if context is not None else Context()
        if trace.get_current_span(ctx).get_span_context().is_valid:
            return ctx  # traceparent sudah dipakai
        values = getter.get(carrier, X_TRACE_ID)
        trace_id = parse_trace_id(values[0].strip().lower()) if values else None
        if trace_id is None:
            return ctx
        parent = SpanContext(
            trace_id=trace_id,
            span_id=RandomIdGenerator().generate_span_id(),
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
        return trace.set_span_in_context(NonRecordingSpan(parent), ctx)

    def inject(self, carrier: CarrierT, context: Context | None = None, setter: Setter = default_setter) -> None:
        span_context = trace.get_current_span(context).get_span_context()
        if span_context.is_valid:
            setter.set(carrier, X_TRACE_ID, format(span_context.trace_id, "032x"))

    @property
    def fields(self) -> set[str]:
        return {X_TRACE_ID}


def install_provider(
    service: str,
    *,
    exporter: SpanExporter | None = None,
    processors: Sequence[SpanProcessor] = (),
    attributes: dict[str, str] | None = None,
) -> TracerProvider:
    """Pasang TracerProvider global (sekali per proses) dan propagator W3C + X-Trace-Id."""
    provider = TracerProvider(
        resource=Resource.create({"service.name": f"astria-{service}", **(attributes or {})}),
        id_generator=AstriaIdGenerator(),
    )
    if exporter is not None:
        provider.add_span_processor(BatchSpanProcessor(exporter))
    for processor in processors:
        provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)
    propagate.set_global_textmap(CompositePropagator([TraceContextTextMapPropagator(), XTraceIdPropagator()]))
    return provider
