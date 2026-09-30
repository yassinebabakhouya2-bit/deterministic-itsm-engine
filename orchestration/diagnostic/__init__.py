"""Deterministic diagnostic state machine (see the architecture document
"Architecture - Agentic RAG diagnostic ITSM"). Pure logic, no network: every
side effect (LLM extraction, OCR, retrieval, plan generation) is injected as a
port, so the whole machine is testable and replayable."""
