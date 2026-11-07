"""Synthetic requirements the 'simulate external webhook' action (key `s`) writes, one per press."""

SIM_POOL = [
    {"id": "ALERT-300", "text": "REQUIREMENT: Alert Deduplication Window\n\nRepeated alerts for the same service should be merged.\n\nThe merge window must be reasonable for the alert severity.\n\nEngineers shall be able to see how many alerts were merged.\n\nCritical alerts should never be silently dropped.\n"},
    {"id": "SEARCH-410", "text": "REQUIREMENT: Search Autocomplete\n\nThe search box shall suggest completions as the user types.\n\nSuggestions must appear quickly.\n\nThe system should rank suggestions by relevance.\n\nSuggestions must not expose items the user may not access.\n"},
    {"id": "SESSION-330", "text": "REQUIREMENT: Session Timeout\n\nIdle sessions shall be ended automatically.\n\nUsers must be warned before their session ends.\n\nThe timeout should be appropriate for the sensitivity of the data.\n\nUnsaved work should be preserved where possible.\n"},
    {"id": "RATE-640", "text": "REQUIREMENT: API Rate Limiting\n\nThe API shall limit requests per client.\n\nLimits must be fair across clients.\n\nClients that exceed the limit should receive a clear response.\n\nTemporary bursts should be tolerated.\n"},
]
