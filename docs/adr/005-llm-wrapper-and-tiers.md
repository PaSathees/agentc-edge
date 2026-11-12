# ADR-005: One `llm()` call site and model tiers

Status: accepted
Date: 2026-11-12
Serves: FR-27, FR-28, NFR-01, NFR-14, NFR-18

## Context

AgentC called two providers from many nodes, with Opus-class models for routine steps and tracing spread across several helpers. Cost was hard to see and harder to cap.

## Options
1. Each agent calls the provider SDK directly.
2. Every model call goes through one function, `llm(tier, messages, tools)`, which picks the model from config, logs the call and its cost, enforces the budget, and removes private addresses from prompts.

## Decision

Option 2. Tiers: small for the Responder and Assistant, mid for the Planner. Claude models in v1; names live in config.

## Consequences

- Changing provider or model is one config value (NFR-18); local models (E3) plug in behind the same call.
- Every call is traced with tokens, USD and latency (FR-27), and the cap is enforced in one place (FR-28).
- Privacy is enforced once: no IP or MAC reaches a model (NFR-14).
- Provider-specific features (prompt caching, batch API) must be exposed as `llm()` options, not used directly.
